import socket
import threading
import time
import sys
import json
import re
import logging
import os
import sqlite3
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
STATIC_DIR = os.path.join(_BASE_DIR, 'aircat-server-py', 'static')
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

def setup_logger():
    """配置日志，避免重复添加Handler"""
    logger = logging.getLogger('PhicommM1 Server')
    level = _LEVEL_MAP.get(LOG_LEVEL, logging.DEBUG)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s : %(message)s')

    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件输出（默认关闭）
    if LOG_FILE:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, time.strftime('%Y-%m-%d') + '.log')
        file_handler = logging.FileHandler(filename=log_file, encoding='utf8')
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
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
        _log(f"SQLite database initialized at {db_path}", 0)

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

    def insert(self, humidity, temperature, pm25, hcho, client_ip):
        """插入一条数据记录（线程安全）"""
        with self.lock:
            self.conn.execute(
                'INSERT INTO sensor_data (humidity, temperature, pm25, hcho, client_ip) VALUES (?, ?, ?, ?, ?)',
                (humidity, temperature, pm25, hcho, client_ip)
            )
            self.conn.commit()

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


# 全局数据库实例（在 main 中初始化）
db_manager = None


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

# MIME 类型映射
_MIME_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.ttf': 'font/ttf',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.ico': 'image/x-icon',
}


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

    def _serve_static(self, rel_path):
        """提供静态文件服务（防止路径穿越）"""
        full_path = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
        static_root = os.path.normpath(STATIC_DIR)
        if not full_path.startswith(static_root + os.sep) and full_path != static_root:
            self.send_error(403, 'Forbidden')
            return
        if not os.path.isfile(full_path):
            self.send_error(404, 'Not Found')
            return
        ext = os.path.splitext(full_path)[1].lower()
        mime = _MIME_TYPES.get(ext, 'application/octet-stream')
        try:
            with open(full_path, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            _log(f"Static file error: {full_path} - {e}", 2)
            self.send_error(500, 'Internal Server Error')

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

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
        elif path.startswith('/static/'):
            self._serve_static(path[len('/static/'):])
        else:
            self.send_error(404, 'Not Found')


def start_web_server(port=WEB_PORT):
    """启动 HTTP Web 服务器（在独立线程中运行）"""
    server = ThreadingHTTPServer(('0.0.0.0', port), WebRequestHandler)
    server.daemon_threads = True
    _log(f"Web server started on port {port}", 0)
    server.serve_forever()


# ========== Socket服务 ==========
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
    # 初始化数据库
    db_manager = DatabaseManager(DB_PATH)

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
