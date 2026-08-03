import socket
import threading
import time
import sys
import json
import re
import logging
import os
import sqlite3
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ========== 配置区 ==========
time_sleep = 5        # 采集间隔（秒）
SOCKET_PORT = 9000    # 监听端口
BUFFER_SIZE = 4096    # 接收缓冲区（增大以处理更大数据包）
RECV_TIMEOUT = 10     # 单次接收超时时间（秒），缩短以快速检测网络问题
MAX_RETRY = 3         # 超时最大重试次数，超过则断开连接重连

# M1设备查询指令（保持原样）
GET_MSG = b'\xaaO\x01%F\x119\x8f\x0b\x00\x00\x00\x00\x00\x00\x00\x00\xb0\xf8\x93\x11dR\x007\x00\x00\x02{"type":5,"status":1}\xff#END#'

# Web/SQLite 配置（通过环境变量控制）
DB_PATH = os.environ.get('DB_PATH', '/data/aircat.db')
WEB_PORT = int(os.environ.get('WEB_PORT', '8080'))
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_FILE = os.path.join(_BASE_DIR, 'aircat-server-py', 'templates', 'sqlite.html')

# ========== 日志配置（通过环境变量控制） ==========
# LOG_LEVEL: DEBUG/INFO/WARNING/ERROR, 默认 DEBUG（控制台输出含DEBUG）
# LOG_FILE: true/false, 默认 false（关闭日志文件）
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'DEBUG').upper()
LOG_FILE = os.environ.get('LOG_FILE', 'false').lower() == 'true'

_LEVEL_MAP = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR
}

# 日志格式（供运行时新增 Handler 复用）
_LOG_FORMATTER = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s : %(message)s')


def setup_logger():
    """配置日志，避免重复添加Handler"""
    logger = logging.getLogger('PhicommM1 Server')
    level = _LEVEL_MAP.get(LOG_LEVEL, logging.DEBUG)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(_LOG_FORMATTER)
    logger.addHandler(console_handler)

    # 文件输出（默认关闭）
    if LOG_FILE:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, time.strftime('%Y-%m-%d') + '.log')
        file_handler = logging.FileHandler(filename=log_file, encoding='utf8')
        file_handler.setLevel(level)
        file_handler.setFormatter(_LOG_FORMATTER)
        logger.addHandler(file_handler)

    return logger

# 全局logger实例
logger = setup_logger()

def _log(message, level=0):
    """简化日志调用"""
    levels = {
        0: logger.info,
        1: logger.warning,
        2: logger.error,
        3: logger.debug
    }
    levels.get(level, logger.info)(message)


def apply_log_settings():
    """根据数据库中的当前设置，立即更新 logger 级别与文件输出"""
    if db_manager is None:
        return
    try:
        level_str = str(db_manager.get_setting('log_level')).upper()
        log_file_val = db_manager.get_setting('log_file')
    except Exception as e:
        _log(f"apply_log_settings read settings failed: {e}", 2)
        return

    level = _LEVEL_MAP.get(level_str, logging.DEBUG)
    logger.setLevel(level)
    # 同步已有 Handler 的级别
    for h in logger.handlers:
        h.setLevel(level)

    # 处理文件 Handler 的动态增删
    has_file_handler = any(isinstance(h, logging.FileHandler) for h in logger.handlers)
    if log_file_val == 1 and not has_file_handler:
        try:
            log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, time.strftime('%Y-%m-%d') + '.log')
            file_handler = logging.FileHandler(filename=log_file, encoding='utf8')
            file_handler.setLevel(level)
            file_handler.setFormatter(_LOG_FORMATTER)
            logger.addHandler(file_handler)
            _log(f"File logging enabled: {log_file}", 3)
        except Exception as e:
            _log(f"Failed to add file handler: {e}", 2)
    elif log_file_val == 0 and has_file_handler:
        for h in logger.handlers[:]:
            if isinstance(h, logging.FileHandler):
                logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
                _log("File logging disabled", 3)


# ========== 工具函数 ==========
def cut(num, decimals):
    """
    截断小数位（不进行四舍五入）
    修复原版本返回字符串的问题，改为返回float
    """
    if not isinstance(num, (int, float)):
        return 0.0

    factor = 10 ** decimals
    return int(num * factor) / factor


# ========== SQLite 数据库管理 ==========
# 设置项的类型映射（用于 get_setting 返回正确的类型）
_SETTING_TYPES = {
    'max_records': int,
    'retention_days': int,
    'auth_enabled': int,
    'auth_user': str,
    'auth_pass': str,
    'log_level': str,
    'log_file': int,
    'm1_brightness': str,
    'm1_timer_enabled': int,
    'm1_timer_day_brightness': int,
    'm1_timer_night_brightness': int,
    'm1_timer_day_start': str,
    'm1_timer_night_start': str,
}

# 设置项的默认值（log_level / log_file 初始从环境变量读取）
_SETTING_DEFAULTS = {
    'max_records': 10000,
    'retention_days': 30,
    'auth_enabled': 0,
    'auth_user': '',
    'auth_pass': '',
    'log_level': os.environ.get('LOG_LEVEL', 'DEBUG').upper(),
    'log_file': 1 if os.environ.get('LOG_FILE', 'false').lower() == 'true' else 0,
    'm1_brightness': '-1',
    'm1_timer_enabled': 0,
    'm1_timer_day_brightness': 100,
    'm1_timer_night_brightness': 0,
    'm1_timer_day_start': '07:00',
    'm1_timer_night_start': '23:00',
}

# 清理后台线程的执行间隔（秒）
CLEANUP_INTERVAL = 300  # 5 分钟


class DatabaseManager:
    """SQLite 数据库管理器（线程安全）"""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        # 确保数据库目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self.lock = threading.Lock()
        # check_same_thread=False 允许跨线程访问
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self._init_settings_table()
        _log(f"SQLite database initialized at {db_path}", 0)
        # 启动后台清理线程
        self._start_cleanup_thread()

    def _init_db(self):
        """初始化数据表"""
        with self.lock:
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS sensor_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    humidity REAL,
                    temperature REAL,
                    pm25 INTEGER,
                    hcho REAL,
                    client_ip TEXT
                )
            ''')
            self.conn.commit()

    def _init_settings_table(self):
        """初始化设置表并写入默认值（首次启动时支持环境变量覆盖）"""
        with self.lock:
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')

            # 判断是否为首次启动（表中无任何设置）
            cur = self.conn.execute('SELECT COUNT(*) FROM settings')
            first_start = cur.fetchone()[0] == 0

            defaults = dict(_SETTING_DEFAULTS)
            if first_start:
                # 首次启动时支持通过环境变量设置用户名/密码
                auth_user_env = os.environ.get('AUTH_USER')
                auth_pass_env = os.environ.get('AUTH_PASS')
                if auth_user_env is not None:
                    defaults['auth_user'] = auth_user_env
                if auth_pass_env is not None:
                    defaults['auth_pass'] = auth_pass_env
                    defaults['auth_enabled'] = 1
                # 首次启动时支持通过环境变量设置 M1 亮度/定时
                m1_brightness_env = os.environ.get('M1_BRIGHTNESS')
                if m1_brightness_env is not None:
                    defaults['m1_brightness'] = m1_brightness_env
                m1_timer_enabled_env = os.environ.get('M1_TIMER_ENABLED')
                if m1_timer_enabled_env is not None:
                    defaults['m1_timer_enabled'] = 1 if m1_timer_enabled_env.lower() in ('1', 'true', 'yes', 'on') else 0

            # 仅插入尚不存在的键（保留已有配置，重启后不丢失）
            for key, value in defaults.items():
                cur = self.conn.execute('SELECT value FROM settings WHERE key=?', (key,))
                if cur.fetchone() is None:
                    self.conn.execute(
                        'INSERT INTO settings (key, value) VALUES (?, ?)',
                        (key, str(value))
                    )
            self.conn.commit()

    # ---------- 设置读写 ----------
    def get_setting(self, key):
        """读取单个设置项，按类型映射返回正确的类型"""
        with self.lock:
            cur = self.conn.execute('SELECT value FROM settings WHERE key=?', (key,))
            row = cur.fetchone()
        if row is None:
            return _SETTING_DEFAULTS.get(key)
        raw = row[0]
        type_fn = _SETTING_TYPES.get(key, str)
        if type_fn is int:
            try:
                return int(raw)
            except (ValueError, TypeError):
                return _SETTING_DEFAULTS.get(key, 0)
        return raw

    def set_setting(self, key, value):
        """更新单个设置项（upsert）"""
        with self.lock:
            self.conn.execute(
                'INSERT INTO settings (key, value) VALUES (?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, str(value))
            )
            self.conn.commit()

    def get_all_settings(self):
        """返回全部设置项的字典（按类型映射）"""
        result = {}
        for key in _SETTING_DEFAULTS:
            result[key] = self.get_setting(key)
        return result

    # ---------- 数据读写 ----------
    def insert(self, humidity, temperature, pm25, hcho, client_ip):
        """插入一条数据记录（线程安全），插入后执行清理策略"""
        with self.lock:
            self.conn.execute(
                'INSERT INTO sensor_data (humidity, temperature, pm25, hcho, client_ip) VALUES (?, ?, ?, ?, ?)',
                (humidity, temperature, pm25, hcho, client_ip)
            )
            self.conn.commit()
        # 锁已释放，执行清理（读取当前设置）
        self.cleanup_data()

    def get_latest(self):
        """获取最新一条记录"""
        with self.lock:
            cur = self.conn.execute(
                'SELECT * FROM sensor_data ORDER BY id DESC LIMIT 1'
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_history(self, hours=24):
        """获取指定小时数内的历史记录"""
        with self.lock:
            cur = self.conn.execute(
                'SELECT * FROM sensor_data WHERE timestamp >= datetime("now", ?) ORDER BY id ASC',
                (f'-{hours} hours',)
            )
            return [dict(r) for r in cur.fetchall()]

    def get_record_count(self):
        """获取当前数据记录总数"""
        with self.lock:
            cur = self.conn.execute('SELECT COUNT(*) FROM sensor_data')
            return cur.fetchone()[0]

    def cleanup_data(self):
        """按保留策略清理数据：删除超过保留期的记录，并裁剪超出上限的旧记录。返回被删除的条数"""
        deleted = 0
        max_records = self.get_setting('max_records')
        retention_days = self.get_setting('retention_days')

        with self.lock:
            # 1. 删除超过保留期的记录
            if retention_days and retention_days > 0:
                cur = self.conn.execute(
                    'DELETE FROM sensor_data WHERE timestamp < datetime("now", ?)',
                    (f'-{retention_days} days',)
                )
                deleted += cur.rowcount

            # 2. 删除超出上限的旧记录
            if max_records and max_records > 0:
                cur = self.conn.execute('SELECT COUNT(*) FROM sensor_data')
                count = cur.fetchone()[0]
                if count > max_records:
                    excess = count - max_records
                    self.conn.execute(
                        'DELETE FROM sensor_data WHERE id IN ('
                        'SELECT id FROM sensor_data ORDER BY id ASC LIMIT ?'
                        ')',
                        (excess,)
                    )
                    deleted += excess
            self.conn.commit()
        return deleted

    def clear_all_data(self):
        """清空全部传感器数据，返回被删除的条数"""
        with self.lock:
            cur = self.conn.execute('DELETE FROM sensor_data')
            self.conn.commit()
            return cur.rowcount

    # ---------- 后台清理线程 ----------
    def _start_cleanup_thread(self):
        """启动后台守护线程，定期执行清理"""
        t = threading.Thread(target=self._cleanup_loop, daemon=True)
        t.start()
        _log("Background cleanup thread started (interval=300s)", 3)

    def _cleanup_loop(self):
        """后台清理循环：每 5 分钟读取当前设置并执行清理"""
        while True:
            try:
                time.sleep(CLEANUP_INTERVAL)
                deleted = self.cleanup_data()
                if deleted > 0:
                    _log(f"Cleanup: deleted {deleted} expired/over-limit records", 3)
            except Exception as e:
                _log(f"Cleanup thread error: {e}", 2)


# 全局数据库实例（在 main 中初始化）
db_manager = None


# ========== 认证系统 ==========
# token -> 过期时间戳（概念上为带过期的 token 集合）
_auth_tokens = {}
_auth_lock = threading.Lock()
TOKEN_EXPIRY = 3600  # 1 小时


def generate_token(username, password):
    """生成简单的 token：md5(username + password + timestamp)"""
    raw = (str(username) + str(password) + str(time.time())).encode('utf-8')
    return hashlib.md5(raw).hexdigest()


def add_token(token):
    """登记一个 token，1 小时后过期"""
    with _auth_lock:
        _auth_tokens[token] = time.time() + TOKEN_EXPIRY


def is_valid_token(token):
    """校验 token 是否有效（存在且未过期）"""
    if not token:
        return False
    with _auth_lock:
        expiry = _auth_tokens.get(token)
        if expiry is None:
            return False
        if time.time() > expiry:
            _auth_tokens.pop(token, None)
            return False
        return True


def cleanup_tokens():
    """清理已过期的 token"""
    with _auth_lock:
        now = time.time()
        expired = [t for t, exp in _auth_tokens.items() if now > exp]
        for t in expired:
            del _auth_tokens[t]


# ========== HTTP Web 服务 ==========
# 启动时一次性读取首页模板
_INDEX_HTML = None
try:
    with open(TEMPLATE_FILE, 'r', encoding='utf-8') as f:
        _INDEX_HTML = f.read()
    _log(f"Loaded template {TEMPLATE_FILE}", 3)
except Exception as e:
    _log(f"Failed to load template {TEMPLATE_FILE}: {e}", 1)
    _INDEX_HTML = '<html><body><h1>M1 Air Monitor</h1><p>Template not found.</p></body></html>'

# 允许通过 /api/settings 更新的键
_SETTING_KEYS = [
    'max_records', 'retention_days', 'auth_enabled',
    'auth_user', 'auth_pass', 'log_level', 'log_file',
    'm1_brightness', 'm1_timer_enabled', 'm1_timer_day_brightness',
    'm1_timer_night_brightness', 'm1_timer_day_start', 'm1_timer_night_start'
]


class WebRequestHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    # 使用自定义 logger，关闭默认日志输出
    def log_message(self, format, *args):
        _log(f"HTTP {self.client_address[0]} - {format % args}", 3)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode('utf-8') if isinstance(html, str) else html
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        """读取并解析请求体 JSON，返回 dict 或 None"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
        except (ValueError, TypeError):
            content_length = 0
        body = self.rfile.read(content_length) if content_length else b''
        if not body:
            return None
        try:
            return json.loads(body.decode('utf-8'))
        except Exception:
            return None

    def _is_authorized(self):
        """鉴权：auth 关闭时放行；开启时校验 Bearer token"""
        if db_manager is None:
            return True
        try:
            if db_manager.get_setting('auth_enabled') != 1:
                return True
        except Exception:
            return True
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[len('Bearer '):].strip()
            return is_valid_token(token)
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # 鉴权：除 /api/login（POST）外，所有路由均需校验
        if not self._is_authorized():
            self._send_json({'error': 'unauthorized'}, 401)
            return

        if path == '/' or path == '/index.html':
            self._send_html(_INDEX_HTML)
        elif path == '/api/latest':
            if db_manager:
                record = db_manager.get_latest()
                if record:
                    record['timestamp'] = str(record.get('timestamp', ''))
                else:
                    record = {}
                self._send_json(record)
            else:
                self._send_json({'error': 'database not initialized'}, 503)
        elif path == '/api/history':
            if db_manager:
                hours_str = query.get('hours', ['24'])[0]
                try:
                    hours = int(hours_str)
                except ValueError:
                    hours = 24
                records = db_manager.get_history(hours)
                for r in records:
                    r['timestamp'] = str(r.get('timestamp', ''))
                self._send_json(records)
            else:
                self._send_json({'error': 'database not initialized'}, 503)
        elif path == '/api/settings':
            if db_manager:
                self._send_json(db_manager.get_all_settings())
            else:
                self._send_json({'error': 'database not initialized'}, 503)
        else:
            self.send_error(404, 'Not Found')

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # /api/login 无需鉴权
        if path == '/api/login':
            data = self._read_json_body()
            if not isinstance(data, dict):
                self._send_json({'success': False}, 401)
                return
            username = data.get('username', '')
            password = data.get('password', '')
            auth_user = db_manager.get_setting('auth_user') if db_manager else ''
            auth_pass = db_manager.get_setting('auth_pass') if db_manager else ''
            if auth_user != '' and username == auth_user and password == auth_pass:
                token = generate_token(username, password)
                add_token(token)
                cleanup_tokens()
                self._send_json({'token': token, 'success': True})
            else:
                self._send_json({'success': False}, 401)
            return

        # 其余 POST 路由均需鉴权
        if not self._is_authorized():
            self._send_json({'error': 'unauthorized'}, 401)
            return

        if path == '/api/settings':
            if not db_manager:
                self._send_json({'error': 'database not initialized'}, 503)
                return
            data = self._read_json_body()
            if not isinstance(data, dict):
                self._send_json({'error': 'invalid json'}, 400)
                return
            changed = []
            for key in _SETTING_KEYS:
                if key in data:
                    db_manager.set_setting(key, data[key])
                    changed.append(key)
            # 立即应用日志相关设置
            if 'log_level' in changed or 'log_file' in changed:
                apply_log_settings()
            self._send_json({'success': True, 'settings': db_manager.get_all_settings()})
            return

        if path == '/api/cleanup':
            if not db_manager:
                self._send_json({'error': 'database not initialized'}, 503)
                return
            deleted = db_manager.clear_all_data()
            self._send_json({'success': True, 'deleted': deleted})
            return

        self.send_error(404, 'Not Found')


def start_web_server(port=WEB_PORT):
    """启动 HTTP Web 服务器（在独立线程中运行）"""
    server = ThreadingHTTPServer(('0.0.0.0', port), WebRequestHandler)
    server.daemon_threads = True
    _log(f"Web server started on port {port}", 0)
    server.serve_forever()


# ========== Socket服务 ==========
def get_current_brightness(db):
    """根据设置获取当前亮度"""
    m1_brightness = db.get_setting('m1_brightness')
    if m1_brightness is not None and int(m1_brightness) >= 0:
        return int(m1_brightness)

    timer_enabled = db.get_setting('m1_timer_enabled')
    if not timer_enabled:
        return -1

    now = time.localtime()
    current_time = now.tm_hour * 60 + now.tm_min

    def parse_time(t_str):
        h, m = t_str.split(':')
        return int(h) * 60 + int(m)

    day_start = parse_time(db.get_setting('m1_timer_day_start') or '07:00')
    night_start = parse_time(db.get_setting('m1_timer_night_start') or '23:00')

    if day_start <= current_time < night_start:
        return int(db.get_setting('m1_timer_day_brightness') or 100)
    else:
        return int(db.get_setting('m1_timer_night_brightness') or 0)


class M1Server:
    """M1设备TCP服务器"""

    def __init__(self, host='0.0.0.0', port=SOCKET_PORT):
        self.host = host
        self.port = port
        self.server_socket = None
        self.running = False
        self.clients = []  # 跟踪客户端线程

    def start(self):
        """启动服务器"""
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(10)
            self.running = True

            _log(f"Socket server started on {self.host}:{self.port}", 0)

            # 接受连接
            while self.running:
                try:
                    self.server_socket.settimeout(1.0)  # 允许定期检查running状态
                    conn, addr = self.server_socket.accept()

                    # 清理已结束的客户端线程，防止内存泄漏
                    self.clients = [t for t in self.clients if t.is_alive()]

                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(conn, addr),
                        daemon=True
                    )
                    client_thread.start()
                    self.clients.append(client_thread)

                except socket.timeout:
                    continue
                except Exception as e:
                    _log(f"Accept error: {e}", 2)

        except socket.error as msg:
            _log(f"Socket error: {msg}", 2)
            sys.exit(1)
        finally:
            self.stop()

    def _handle_client(self, conn, addr):
        """处理单个客户端连接"""
        _log(f"New connection from {addr}", 0)

        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        conn.settimeout(RECV_TIMEOUT)

        consecutive_timeout = 0
        total_data_count = 0

        try:
            while self.running:
                try:
                    _log(f"Client {addr} sending query...", 3)
                    conn.sendall(GET_MSG)
                    _log(f"Client {addr} query sent, waiting for response...", 3)

                    data = conn.recv(BUFFER_SIZE)

                    if not data:
                        _log(f"Client {addr} closed connection (empty recv)", 0)
                        break

                    conn.settimeout(2)
                    while True:
                        try:
                            chunk = conn.recv(BUFFER_SIZE)
                            if not chunk:
                                break
                            data += chunk
                        except socket.timeout:
                            break

                    conn.settimeout(RECV_TIMEOUT)

                    json_data = self._parse_data(data)
                    if json_data:
                        self._process_data(json_data, addr)
                        total_data_count += 1
                        consecutive_timeout = 0

                        # 亮度控制
                        if db_manager and data and len(data) >= 23:
                            brightness = get_current_brightness(db_manager)
                            if brightness >= 0:
                                try:
                                    brightness_json = json.dumps({"brightness": brightness})
                                    brightness_msg = data[:23] + b'\x00\x18\x00\x00\x02' + brightness_json.encode('utf-8') + b'\xff#END#'
                                    conn.sendall(brightness_msg)
                                    _log(f"Sent brightness control: {brightness} to {addr}", 3)
                                except Exception as e:
                                    _log(f"Brightness control error: {e}", 1)
                    else:
                        _log(f"Client {addr} received data but no valid JSON (len={len(data)})", 1)

                    time.sleep(time_sleep)

                except socket.timeout:
                    consecutive_timeout += 1
                    _log(f"Client {addr} recv timeout ({consecutive_timeout}/{MAX_RETRY}), data received: {total_data_count}", 1)

                    if consecutive_timeout >= MAX_RETRY:
                        _log(f"Client {addr} max recv timeout reached ({MAX_RETRY}), closing connection", 2)
                        break

                    time.sleep(1)
                    continue

                except ConnectionResetError:
                    _log(f"Client {addr} reset connection (ConnectionResetError)", 1)
                    break
                except BrokenPipeError:
                    _log(f"Client {addr} broken pipe (BrokenPipeError)", 1)
                    break
                except ConnectionAbortedError:
                    _log(f"Client {addr} connection aborted", 1)
                    break
                except OSError as e:
                    _log(f"Client {addr} OS error: {e}", 2)
                    break
                except Exception as e:
                    _log(f"Client {addr} unexpected error: {e}", 2)
                    consecutive_timeout += 1
                    if consecutive_timeout >= MAX_RETRY:
                        break
                    time.sleep(1)
                    continue

        except Exception as e:
            _log(f"Client {addr} fatal error: {e}", 2)
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except:
                pass
            conn.close()
            _log(f"Connection closed: {addr}, total data packets: {total_data_count}", 0)

    def _parse_data(self, data):
        """解析JSON数据"""
        try:
            pattern = r"(\{.*?\})"
            matches = re.findall(pattern, data.decode('utf-8', errors='ignore'), re.DOTALL)

            if not matches:
                return None

            # 取最后一个匹配的JSON
            return json.loads(matches[-1])

        except json.JSONDecodeError as e:
            _log(f"JSON parse error: {e}", 2)
            return None
        except Exception as e:
            _log(f"Parse error: {e}", 2)
            return None

    def _process_data(self, json_data, addr):
        """处理解析后的数据"""
        try:
            # 提取字段（带默认值防KeyError）
            humidity = cut(float(json_data.get('humidity', 0)), 1)
            temperature = cut(float(json_data.get('temperature', 0)), 1)
            pm25 = json_data.get('value', 0)
            hcho = cut(float(json_data.get('hcho', 0)) / 1000, 2)
            client_ip = addr[0] if isinstance(addr, tuple) else str(addr)

            _log(f"Data from {addr}: H={humidity}%, T={temperature}°C, PM2.5={pm25}, HCHO={hcho}", 3)

            # 写入 SQLite 数据库
            if db_manager:
                db_manager.insert(humidity, temperature, pm25, hcho, client_ip)
                _log(f"Data saved to database from {client_ip}", 3)

        except (KeyError, ValueError, TypeError) as e:
            _log(f"Data processing error: {e}, data: {json_data}", 2)

    def stop(self):
        """停止服务器"""
        self.running = False
        if self.server_socket:
            self.server_socket.close()
        _log("Server stopped", 0)


# ========== 主程序 ==========
if __name__ == '__main__':
    # ---------- CLI 命令：resetname / resetpasswd ----------
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == 'resetname':
            db_manager = DatabaseManager(DB_PATH)
            try:
                new_user = input('Enter new username: ').strip()
            except EOFError:
                new_user = ''
            if new_user:
                db_manager.set_setting('auth_user', new_user)
                print(f"Username updated to: {new_user}")
            else:
                print("Username not changed (empty input).")
            sys.exit(0)
        elif cmd == 'resetpasswd':
            db_manager = DatabaseManager(DB_PATH)
            try:
                new_pass = input('Enter new password: ').strip()
            except EOFError:
                new_pass = ''
            if new_pass:
                db_manager.set_setting('auth_pass', new_pass)
                print("Password updated successfully.")
            else:
                print("Password not changed (empty input).")
            sys.exit(0)
        # 其它参数则继续走正常启动流程

    # ---------- 正常启动 ----------
    # 初始化数据库
    db_manager = DatabaseManager(DB_PATH)

    # 立即应用数据库中的日志设置
    apply_log_settings()

    # 输出当前 M1 亮度相关设置
    try:
        _log(
            f"M1 brightness settings: brightness={db_manager.get_setting('m1_brightness')}, "
            f"timer_enabled={db_manager.get_setting('m1_timer_enabled')}, "
            f"day_brightness={db_manager.get_setting('m1_timer_day_brightness')}, "
            f"night_brightness={db_manager.get_setting('m1_timer_night_brightness')}, "
            f"day_start={db_manager.get_setting('m1_timer_day_start')}, "
            f"night_start={db_manager.get_setting('m1_timer_night_start')}",
            0
        )
    except Exception as e:
        _log(f"Failed to log M1 brightness settings: {e}", 1)

    # 启动 Web 服务器线程
    web_thread = threading.Thread(target=start_web_server, args=(WEB_PORT,), daemon=True)
    web_thread.start()

    # 启动 Socket 服务器（主线程）
    server = M1Server()

    try:
        server.start()
    except KeyboardInterrupt:
        _log("Received shutdown signal", 0)
        server.stop()
