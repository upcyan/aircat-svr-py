import socket
import threading
import time
import sys
import json
import re
import logging
import os

# ========== 配置区 ==========
time_sleep = 5        # 采集间隔（秒）
SOCKET_PORT = 9000    # 监听端口
BUFFER_SIZE = 4096    # 接收缓冲区（增大以处理更大数据包）
RECV_TIMEOUT = 10     # 单次接收超时时间（秒），缩短以快速检测网络问题
MAX_RETRY = 3         # 超时最大重试次数，超过则断开连接重连

# M1设备查询指令（保持原样）
GET_MSG = b'\xaaO\x01%F\x119\x8f\x0b\x00\x00\x00\x00\x00\x00\x00\x00\xb0\xf8\x93\x11dR\x007\x00\x00\x02{"type":5,"status":1}\xff#END#'

# ========== M1 设备控制（通过环境变量配置） ==========
# 亮度: -1=不控制, 0=息屏, 25/50/75/100=亮度等级
M1_BRIGHTNESS = int(os.environ.get('M1_BRIGHTNESS', '-1'))
# 定时开关屏
M1_TIMER_ENABLED = os.environ.get('M1_TIMER_ENABLED', 'false').lower() == 'true'
M1_TIMER_DAY_BRIGHTNESS = int(os.environ.get('M1_TIMER_DAY_BRIGHTNESS', '100'))
M1_TIMER_NIGHT_BRIGHTNESS = int(os.environ.get('M1_TIMER_NIGHT_BRIGHTNESS', '0'))
M1_TIMER_DAY_START = os.environ.get('M1_TIMER_DAY_START', '07:00')   # 白天开始时间
M1_TIMER_NIGHT_START = os.environ.get('M1_TIMER_NIGHT_START', '23:00')  # 夜晚开始时间

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


# ========== M1 亮度控制 ==========
def get_current_brightness():
    """根据定时设置获取当前亮度"""
    if M1_BRIGHTNESS >= 0:
        # 固定亮度模式
        return M1_BRIGHTNESS

    if not M1_TIMER_ENABLED:
        return -1  # 不控制

    # 定时模式
    now = time.localtime()
    current_time = now.tm_hour * 60 + now.tm_min

    # 解析白天/夜晚开始时间
    def parse_time(t_str):
        h, m = t_str.split(':')
        return int(h) * 60 + int(m)

    day_start = parse_time(M1_TIMER_DAY_START)
    night_start = parse_time(M1_TIMER_NIGHT_START)

    if day_start <= current_time < night_start:
        return M1_TIMER_DAY_BRIGHTNESS
    else:
        return M1_TIMER_NIGHT_BRIGHTNESS


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
            
            _log(f"Server started on {self.host}:{self.port}", 0)
            
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

                    # 处理完数据后，检查亮度控制
                    brightness = get_current_brightness()
                    if brightness >= 0 and data and len(data) >= 23:
                        try:
                            brightness_json = json.dumps({"brightness": brightness})
                            brightness_msg = data[:23] + b'\x00\x18\x00\x00\x02' + brightness_json.encode('utf-8') + b'\xff#END#'
                            conn.sendall(brightness_msg)
                            _log(f"Sent brightness control: {brightness} to {addr}", 3)
                        except Exception as e:
                            _log(f"Brightness control error: {e}", 1)

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
            
            _log(f"Data from {addr}: H={humidity}%, T={temperature}°C, PM2.5={pm25}, HCHO={hcho}", 3)
            
            # 数据仅打印日志，不再写入数据库
            # 如需处理数据，可在此添加其他逻辑（如发送到MQTT、HTTP API等）
                
        except (KeyError, ValueError, TypeError) as e:
            _log(f"Data processing error: {e}, data: {json_data}", 2)
    
    def stop(self):
        """停止服务器"""
        self.running = False
        if self.server_socket:
            self.server_socket.close()
        _log("Server stopped", 0)


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


# ========== 主程序 ==========
if __name__ == '__main__':
    # 打印亮度控制配置
    _log("===== M1 Brightness Control Settings =====", 0)
    _log(f"M1_BRIGHTNESS (fixed): {M1_BRIGHTNESS} (-1=not controlled, 0=off, 25/50/75/100=level)", 0)
    _log(f"M1_TIMER_ENABLED: {M1_TIMER_ENABLED}", 0)
    _log(f"M1_TIMER_DAY_BRIGHTNESS: {M1_TIMER_DAY_BRIGHTNESS}, M1_TIMER_NIGHT_BRIGHTNESS: {M1_TIMER_NIGHT_BRIGHTNESS}", 0)
    _log(f"M1_TIMER_DAY_START: {M1_TIMER_DAY_START}, M1_TIMER_NIGHT_START: {M1_TIMER_NIGHT_START}", 0)
    _log(f"Current resolved brightness: {get_current_brightness()}", 0)
    _log("==========================================", 0)

    server = M1Server()

    try:
        server.start()
    except KeyboardInterrupt:
        _log("Received shutdown signal", 0)
        server.stop()
