import importlib.util
import io
import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server_common import FrameReadError, recv_bounded_frame


def load_web_module():
    spec = importlib.util.spec_from_file_location('aircat_server_web', ROOT / 'aircat-server-web.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_server_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, size):
        if not self.chunks:
            raise socket.timeout()
        return self.chunks.pop(0)


class FrameTests(unittest.TestCase):
    def test_marker_across_chunks_finishes_frame(self):
        conn = FakeSocket([b'ND#', b'extra'])
        result = recv_bounded_frame(
            conn, b'payload#E', buffer_size=16, chunk_timeout=1,
            max_frame_bytes=64, max_frame_seconds=5,
        )
        self.assertEqual(result, b'payload#END#')
        self.assertEqual(conn.chunks, [b'extra'])

    def test_oversized_frame_is_rejected(self):
        conn = FakeSocket([b'6789'])
        with self.assertRaises(FrameReadError):
            recv_bounded_frame(
                conn, b'12345', buffer_size=4, chunk_timeout=1,
                max_frame_bytes=8, max_frame_seconds=5,
            )


class DummyHeaders(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class BodyReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web = load_web_module()

    def make_handler(self, length, body=b'{}'):
        handler = object.__new__(self.web.WebRequestHandler)
        handler.headers = DummyHeaders({'Content-Length': str(length)})
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        handler.responses = []
        handler._send_json = lambda data, status=200: handler.responses.append((status, data))
        return handler

    def test_oversized_http_body_returns_413(self):
        handler = self.make_handler(self.web.MAX_HTTP_BODY_BYTES + 1)
        result = handler._read_json_body()
        self.assertIs(result, self.web._BODY_ERROR)
        self.assertEqual(handler.responses[0][0], 413)
        self.assertTrue(handler.close_connection)

    def test_negative_http_body_returns_400(self):
        handler = self.make_handler(-1)
        result = handler._read_json_body()
        self.assertIs(result, self.web._BODY_ERROR)
        self.assertEqual(handler.responses[0][0], 400)

    def test_valid_json_body_remains_supported(self):
        handler = self.make_handler(2)
        self.assertEqual(handler._read_json_body(), {})


class LoginLimiterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web = load_web_module()

    def setUp(self):
        self.web._login_attempts.clear()

    def test_login_failures_are_rate_limited_and_success_resets(self):
        ip = '192.0.2.10'
        for _ in range(self.web.LOGIN_MAX_FAILURES - 1):
            self.assertEqual(self.web._record_login_result(ip, False), 0)
        self.assertGreater(self.web._record_login_result(ip, False), 0)
        self.assertGreater(self.web._login_retry_after(ip), 0)
        self.web._record_login_result(ip, True)
        self.assertEqual(self.web._login_retry_after(ip), 0)

    def test_login_state_is_bounded(self):
        original_max = self.web.LOGIN_MAX_FAILURES
        self.web.LOGIN_MAX_FAILURES = 999999
        try:
            for index in range(2100):
                self.web._record_login_result(f'198.51.100.{index}', False)
            self.assertLessEqual(len(self.web._login_attempts), 2048)
        finally:
            self.web.LOGIN_MAX_FAILURES = original_max


class AuthenticationBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web = load_web_module()

    def test_public_settings_never_include_password(self):
        class FakeDb:
            def get_all_settings(self):
                return {'auth_user': 'admin', 'auth_pass': 'secret', 'log_file': 1}

        with mock.patch.object(self.web, 'db_manager', FakeDb()):
            result = self.web._public_settings()
        self.assertNotIn('auth_pass', result)
        self.assertEqual(result['username'], 'admin')

    def test_admin_write_requires_a_valid_token_even_when_auth_is_disabled(self):
        handler = object.__new__(self.web.WebRequestHandler)
        handler.headers = DummyHeaders()
        self.assertFalse(handler._is_admin_authorized())
        class FakeDb:
            values = {'auth_enabled': 0, 'auth_user': 'admin', 'auth_pass': 'secret'}

            def get_setting(self, key):
                return self.values[key]

        fake_db = FakeDb()
        with mock.patch.object(self.web, 'db_manager', fake_db):
            token = self.web.generate_token()
            self.assertTrue(self.web.add_token(token))
            handler.headers['Authorization'] = f'Bearer {token}'
            self.assertTrue(handler._is_admin_authorized())
            fake_db.values['auth_pass'] = 'rotated'
            self.assertFalse(handler._is_admin_authorized())


class HttpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web = load_web_module()

    def setUp(self):
        class FakeDb:
            def __init__(self):
                self.values = {
                    'auth_enabled': 1, 'auth_user': 'admin', 'auth_pass': 'secret',
                    'log_file': 0, 'log_level': 'INFO',
                }

            def get_setting(self, key):
                return self.values.get(key, 0)

            def get_all_settings(self):
                return dict(self.values)

            def set_setting(self, key, value):
                self.values[key] = value

            def clear_all_data(self):
                return 3

        self.fake_db = FakeDb()
        self.db_patch = mock.patch.object(self.web, 'db_manager', self.fake_db)
        self.db_patch.start()
        self.web._auth_tokens.clear()
        self.web._login_attempts.clear()
        self.server = self.web.BoundedThreadingHTTPServer(
            ('127.0.0.1', 0), self.web.WebRequestHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.db_patch.stop()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, dict(response.headers), payload

    def test_brightness_settings_validate_before_saving(self):
        token = self.web.generate_token()
        self.web.add_token(token)
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        for invalid in ({'m1_brightness': 101}, {'m1_timer_day_start': '25:00'},
                        {'m1_timer_enabled': 1, 'm1_timer_day_start': '07:00',
                         'm1_timer_night_start': '07:00'}):
            original = dict(self.fake_db.values)
            status, _, _ = self.request('POST', '/api/settings', json.dumps(invalid), headers)
            self.assertEqual(status, 400)
            self.assertEqual(self.fake_db.values, original)
        valid = {'m1_timer_enabled': 1, 'm1_timer_day_start': '22:00',
                 'm1_timer_night_start': '07:00', 'm1_timer_day_brightness': 0}
        status, _, body = self.request('POST', '/api/settings', json.dumps(valid), headers)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['success'])
        self.assertEqual(self.fake_db.get_setting('m1_timer_day_brightness'), 0)

    def test_login_shell_and_admin_boundary(self):
        status, _, _ = self.request('GET', '/')
        self.assertEqual(status, 200)
        status, _, _ = self.request('GET', '/api/settings')
        self.assertEqual(status, 401)
        body = json.dumps({'username': 'admin', 'password': 'secret'})
        status, _, payload = self.request(
            'POST', '/api/login', body, {'Content-Type': 'application/json'}
        )
        self.assertEqual(status, 200)
        token = json.loads(payload)['token']
        status, _, payload = self.request(
            'GET', '/api/settings', headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(status, 200)
        self.assertNotIn('auth_pass', json.loads(payload))
        status, _, _ = self.request('POST', '/api/cleanup')
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            'POST', '/api/cleanup', headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(status, 200)

    def test_oversized_request_is_rejected_before_body_read(self):
        status, _, _ = self.request(
            'POST', '/api/login', b'{}',
            {'Content-Type': 'application/json',
             'Content-Length': str(self.web.MAX_HTTP_BODY_BYTES + 1)},
        )
        self.assertEqual(status, 413)


class StorageBootstrapTests(unittest.TestCase):
    def test_auth_environment_recovers_existing_unauthenticated_database(self):
        import storage_backends

        types = {'auth_enabled': int, 'auth_user': str, 'auth_pass': str}
        defaults = {'auth_enabled': 0, 'auth_user': '', 'auth_pass': ''}
        storage_backends.configure(types, defaults, 3600, lambda *args: None)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'aircat.db')
            with mock.patch.dict('os.environ', {}, clear=True):
                first = storage_backends.SqliteStorage(path)
                self.assertEqual(first.get_setting('auth_enabled'), 0)
                first.close()
            with mock.patch.dict(
                'os.environ', {'AUTH_USER': 'admin', 'AUTH_PASS': 'new-secret'}, clear=True
            ):
                recovered = storage_backends.SqliteStorage(path)
                self.assertEqual(recovered.get_setting('auth_enabled'), 1)
                self.assertEqual(recovered.get_setting('auth_user'), 'admin')
                self.assertEqual(recovered.get_setting('auth_pass'), 'new-secret')
                recovered.close()


class ReconnectTests(unittest.TestCase):
    def assert_reconnects(self, module):
        module.time_sleep = 0.05
        server = module.M1Server(host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.start, daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while (server.server_socket is None or not server.running) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(server.running)
        port = server.server_socket.getsockname()[1]

        payload = b'\xaa{"humidity":40,"temperature":23,"value":10,"hcho":20}\xff#END#'
        for _ in range(2):
            with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
                query = client.recv(4096)
                self.assertEqual(query, module.GET_MSG)
                client.sendall(payload)
            time.sleep(0.1)

        server.stop()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_web_server_accepts_device_after_disconnect(self):
        self.assert_reconnects(load_server_module('aircat_web_reconnect', 'aircat-server-web.py'))

    def test_web_reconnect_replaces_sleeping_connection_at_capacity(self):
        module = load_server_module('aircat_web_replace', 'aircat-server-web.py')
        module.time_sleep = 5
        module.MAX_DEVICE_CLIENTS = 1
        server = module.M1Server(host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.start, daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while (server.server_socket is None or not server.running) and time.monotonic() < deadline:
            time.sleep(0.01)
        port = server.server_socket.getsockname()[1]
        payload = b'\xaa{"humidity":40,"temperature":23,"value":10,"hcho":20}\xff#END#'
        first = socket.create_connection(('127.0.0.1', port), timeout=2)
        self.assertEqual(first.recv(4096), module.GET_MSG)
        first.sendall(payload)
        time.sleep(0.05)
        with socket.create_connection(('127.0.0.1', port), timeout=2) as second:
            self.assertEqual(second.recv(4096), module.GET_MSG)
            second.sendall(payload)
        first.close()
        server.stop()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_lite_server_accepts_device_after_disconnect(self):
        self.assert_reconnects(load_server_module('aircat_lite_reconnect', 'aircat-server-lite.py'))


class BrightnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_web_module()

    def brightness(self, hour, minute=0, **settings):
        db = mock.Mock()
        db.get_setting.side_effect = settings.get
        now = mock.Mock(tm_hour=hour, tm_min=minute)
        with mock.patch.object(self.module.time, 'localtime', return_value=now):
            return self.module.get_current_brightness(db)

    def test_manual_and_disabled(self):
        self.assertEqual(self.brightness(12, m1_brightness='0', m1_timer_enabled=0), 0)
        self.assertEqual(self.brightness(12, m1_timer_enabled='0'), -1)

    def test_timer_overrides_manual_and_preserves_zero(self):
        self.assertEqual(self.brightness(12, m1_brightness='25', m1_timer_enabled=1,
                                        m1_timer_day_brightness=0), 0)

    def test_schedule_boundaries_and_midnight(self):
        settings = dict(m1_timer_enabled=1, m1_timer_day_start='22:00',
                        m1_timer_night_start='07:00', m1_timer_day_brightness=50,
                        m1_timer_night_brightness=-1)
        for hour, expected in ((22, 50), (0, 50), (6, 50), (7, -1), (21, -1)):
            with self.subTest(hour=hour):
                self.assertEqual(self.brightness(hour, **settings), expected)

    def test_control_protocol_uses_device_header_and_correct_length(self):
        frame = bytes(range(24)) + bytes(10)
        frame = b'\xaa' + frame[1:]
        for level in (0, 25, 50, 75, 100):
            packet = self.module.build_brightness_message(frame, level)
            self.assertEqual(packet[:24], frame[:24])
            self.assertEqual(packet[25:28], b'\x00\x00\x02')
            self.assertEqual(packet[-6:], b'\xff#END#')
            self.assertEqual(packet[24], len(packet[28:-6]) + 3)
            self.assertEqual(json.loads(packet[28:-6]), {'brightness': str(level), 'type': 2})
        with self.assertRaises(ValueError):
            self.module.build_brightness_message(frame, -1)
        with self.assertRaises(ValueError):
            self.module.build_brightness_message(b'not a device frame', 25)


class ConnectionSetupRaceTests(unittest.TestCase):
    def test_closed_socket_before_worker_starts_preserves_replacement_and_slot(self):
        module = load_web_module()
        server = module.M1Server()
        old = socket.socket()
        replacement = socket.socket()
        ip = '127.0.0.1'
        try:
            server._register_conn(ip, old)
            server._register_conn(ip, replacement)
            old.close()  # Reproduce accept-loop replacement before worker startup.
            server._client_slots = threading.BoundedSemaphore(1)
            self.assertTrue(server._client_slots.acquire(blocking=False))
            with mock.patch.object(module, '_log') as log:
                server._handle_client_with_slot(old, (ip, 12345))
            self.assertIs(server._conn_map[ip], replacement)
            self.assertTrue(server._client_slots.acquire(blocking=False))
            self.assertTrue(any('closed during setup' in call.args[0] for call in log.call_args_list))
            self.assertTrue(any('Connection closed:' in call.args[0] for call in log.call_args_list))
        finally:
            old.close()
            replacement.close()


if __name__ == '__main__':
    unittest.main()
