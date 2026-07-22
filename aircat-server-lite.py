import socket
import threading
import time
import sys
import json
import re
import logging
import os
from common import function

# ========== 配置区 ==========
time_sleep = 5        # 采集间隔（秒）
SOCKET_PORT = 9000    # 监听端口
BUFFER_SIZE = 4096    # 接收缓冲区（增大以处理更大数据包）
RECV_TIMEOUT = 30     # 接收超时时间（秒），增加容错性

# M1设备查询指令（保持原样）
GET_MSG = b'\xaaO\x01%F\x119\x8f\x0b\x00\x00\x00\x00\x00\x00\x00\x00\xb0\xf8\x93\x11dR\x007\x00\x00\x02{"type":5,"status":1}\xff#END#'

# ========== 日志配置（修复重复Handler问题） ==========
def setup_logger():
    """配置日志，避免重复添加Handler"""
    logger = logging.getLogger('PhicommM1 Server')
    logger.setLevel(logging.DEBUG)
    
    # 如果已有Handler，不再添加
    if logger.handlers:
        return logger
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s : %(message)s')
    
    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件输出
    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
    os.makedirs(log_dir, exist_ok=True)  # 自动创建目录
    
    log_file = os.path.join(log_dir, time.strftime('%Y-%m-%d') + '.log')
    file_handler = logging.FileHandler(filename=log_file, encoding='utf8')
    file_handler.setLevel(logging.DEBUG)
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
        
        try:
            while self.running:
                try:
                    # 发送查询指令
                    conn.sendall(GET_MSG)
                    
                    # 接收数据（带超时）
                    conn.settimeout(RECV_TIMEOUT)
                    data = conn.recv(BUFFER_SIZE)
                    
                    if not data:
                        _log(f"Client {addr} disconnected", 0)
                        break
                    
                    # 循环接收直到获取完整数据（处理部分读取）
                    conn.settimeout(2)  # 后续接收超时缩短
                    while True:
                        try:
                            chunk = conn.recv(BUFFER_SIZE)
                            if not chunk:
                                break
                            data += chunk
                        except socket.timeout:
                            # 后续数据接收超时表示已接收完
                            break
                    
                    # 解析数据
                    json_data = self._parse_data(data)
                    if json_data:
                        self._process_data(json_data, addr)
                    
                    # 等待下一次采集
                    time.sleep(time_sleep)
                    
                except socket.timeout:
                    # 超时不中断连接，继续下一轮循环
                    _log(f"Client {addr} timeout, continuing...", 1)
                    continue
                except ConnectionResetError:
                    _log(f"Client {addr} reset connection", 1)
                    break
                except Exception as e:
                    _log(f"Client {addr} error: {e}", 2)
                    # 非致命错误继续循环
                    continue
                    
        except Exception as e:
            _log(f"Client {addr} fatal error: {e}", 2)
        finally:
            conn.close()
            _log(f"Connection closed: {addr}", 0)
    
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
    server = M1Server()
    
    try:
        server.start()
    except KeyboardInterrupt:
        _log("Received shutdown signal", 0)
        server.stop()
