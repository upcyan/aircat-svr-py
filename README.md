# 斐讯悟空 M1 服务器

> **Lite 版**：[upcyan/aircat-server-lite](https://hub.docker.com/r/upcyan/aircat-server-lite) — 轻量级数据采集
>
> **SQLite 版**：[upcyan/aircat-server-sqlite](https://hub.docker.com/r/upcyan/aircat-server-sqlite) — 数据存储 + Web 可视化界面
>
> 支持架构：`linux/amd64` · `linux/arm64`

基于 Docker 和 Python 的斐讯悟空（Phicomm AirCat）M1 设备数据采集服务器，提供两个版本：

| 版本 | 说明 | 适用场景 |
|------|------|----------|
| **Lite** | 仅采集数据并输出日志 | 轻量部署、二次开发 |
| **SQLite** | 采集数据存入 SQLite + Web 界面展示 | 开箱即用、数据可视化 |

## 功能特性

### 通用功能
- 监听 TCP Socket 端口，接收 M1 设备上报的环境数据
- 解析设备数据：湿度、温度、PM2.5、甲醛（HCHO）
- Docker 容器化部署，一键启动
- 支持多客户端并发连接
- 自动断线重连机制
- 多架构镜像支持（x86 / ARM64）
- 日志级别和日志文件可通过环境变量控制

### SQLite 版独有功能
- 数据自动存入 SQLite 数据库，支持持久化
- 内置 Web 管理界面，实时查看各项数据
- 历史数据折线图（ECharts），支持点击图例隐藏/显示各项数据
- 时间范围切换：1小时 / 6小时 / 24小时 / 7天
- 实时数据自动刷新（5秒更新卡片，60秒更新图表）

## 技术栈

- **语言**: Python 3.14
- **框架**: 原生 Socket + http.server（SQLite 版）
- **数据库**: SQLite（SQLite 版）
- **前端**: ECharts 5（SQLite 版）
- **容器**: Docker / Docker Compose
- **CI/CD**: GitHub Actions 自动构建双镜像

## 项目结构

```
aircat-svr-py/
├── aircat-server-lite.py          # Lite 版主程序
├── aircat-server-sqlite.py        # SQLite 版主程序（Socket + Web + SQLite）
├── aircat-server-py/
│   ├── common/
│   │   ├── function.py            # 工具函数
│   │   └── sql.conf               # 配置文件
│   ├── static/                    # 静态资源（CSS、JS、图片）
│   └── templates/
│       └── sqlite.html            # SQLite 版 Web 界面
├── docker-yaml/
│   ├── docker-lite/
│   │   └── docker-compose.yml     # Lite 版 Docker Compose
│   └── docker-sqlite/
│       └── docker-compose.yml     # SQLite 版 Docker Compose
├── lite.Dockerfile                # Lite 版 Docker 镜像
├── sqlite.Dockerfile              # SQLite 版 Docker 镜像
├── VERSION                        # 版本号
└── README.md                      # 项目说明文档
```

## 快速开始

### Lite 版（轻量级）

#### Docker 部署

```bash
# 拉取镜像
docker pull upcyan/aircat-server-lite:latest

# 运行容器
docker run -d \
  --name aircat-server-lite \
  -p 9000:9000 \
  -e TZ=Asia/Shanghai \
  -e LOG_LEVEL=DEBUG \
  -e LOG_FILE=false \
  --restart always \
  upcyan/aircat-server-lite:latest

# 查看日志
docker logs -f aircat-server-lite
```

#### Docker Compose 部署

```yaml
services:
  aircat-server-lite:
    image: upcyan/aircat-server-lite:latest
    container_name: aircat-server-lite
    ports:
      - "9000:9000"
    environment:
      - TZ=Asia/Shanghai
      - LOG_LEVEL=DEBUG
      - LOG_FILE=false
    restart: always
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

---

### SQLite 版（数据存储 + Web 界面）

#### Docker 部署

```bash
# 拉取镜像
docker pull upcyan/aircat-server-sqlite:latest

# 运行容器
docker run -d \
  --name aircat-server-sqlite \
  -p 9000:9000 \
  -p 8080:8080 \
  -e TZ=Asia/Shanghai \
  -e LOG_LEVEL=DEBUG \
  -e LOG_FILE=false \
  -e DB_PATH=/data/aircat.db \
  -e WEB_PORT=8080 \
  -v ./data:/data \
  --restart always \
  upcyan/aircat-server-sqlite:latest

# 查看 Web 界面
# 浏览器访问 http://服务器IP:8080

# 查看日志
docker logs -f aircat-server-sqlite
```

#### Docker Compose 部署

```yaml
services:
  aircat-server-sqlite:
    image: upcyan/aircat-server-sqlite:latest
    container_name: aircat-server-sqlite
    ports:
      - "9000:9000"
      - "8080:8080"
    environment:
      - TZ=Asia/Shanghai
      - LOG_LEVEL=DEBUG
      - LOG_FILE=false
      - DB_PATH=/data/aircat.db
      - WEB_PORT=8080
    volumes:
      - ./data:/data
    restart: always
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

启动后访问 `http://服务器IP:8080` 即可查看 Web 界面。

### 本地构建

```bash
# Lite 版
docker build -t aircat-server-lite -f lite.Dockerfile .

# SQLite 版
docker build -t aircat-server-sqlite -f sqlite.Dockerfile .
```

### 直接运行 Python

```bash
# Lite 版
python aircat-server-lite.py

# SQLite 版
python aircat-server-sqlite.py
```

## 配置说明

### 服务器运行参数

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 监听端口 | 9000 | TCP Socket 端口 |
| 采集间隔 | 5 秒 | 设备数据采集频率 |
| 接收缓冲区 | 4096 字节 | Socket 接收缓冲区大小 |
| 接收超时 | 10 秒 | 数据接收超时时间 |
| 最大重试次数 | 3 次 | 超时后最大重试次数 |

### 通用环境变量

| 环境变量 | 默认值 | 可选值 | 说明 |
|----------|--------|--------|------|
| `LOG_LEVEL` | `DEBUG` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | 控制台日志级别 |
| `LOG_FILE` | `false` | `true` / `false` | 是否写入日志文件 |

### SQLite 版独有环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `DB_PATH` | `/data/aircat.db` | SQLite 数据库文件路径 |
| `WEB_PORT` | `8080` | Web 界面端口 |

### 配置示例

- **调试排查**：`LOG_LEVEL=DEBUG` + `LOG_FILE=false`（默认），通过 `docker logs -f` 查看所有日志
- **生产环境**：`LOG_LEVEL=INFO` + `LOG_FILE=false`，仅输出重要信息
- **持久化日志**：`LOG_LEVEL=DEBUG` + `LOG_FILE=true`，挂载 `./logs:/logs` 目录保存日志文件
- **数据持久化**（SQLite 版）：挂载 `./data:/data` 目录，数据库文件持久保存到宿主机

## 数据格式

服务器接收 M1 设备上报的 JSON 数据，包含以下字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| humidity | float | 湿度（%） |
| temperature | float | 温度（°C） |
| value | int | PM2.5 值（μg/m³） |
| hcho | float | 甲醛浓度（mg/m³） |

### SQLite 数据表结构

```sql
CREATE TABLE sensor_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    humidity REAL,        -- 湿度（%）
    temperature REAL,     -- 温度（°C）
    pm25 INTEGER,         -- PM2.5（μg/m³）
    hcho REAL,            -- 甲醛（mg/m³）
    client_ip TEXT        -- 设备 IP 地址
);
```

## Web API（SQLite 版）

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | Web 界面页面 |
| `/api/latest` | GET | 获取最新一条数据记录 |
| `/api/history?hours=24` | GET | 获取指定小时数内的历史数据 |

## Docker Hub 仓库

| 仓库 | 说明 |
|------|------|
| [upcyan/aircat-server-lite](https://hub.docker.com/r/upcyan/aircat-server-lite) | 斐讯悟空 M1 轻量级数据采集服务器 |
| [upcyan/aircat-server-sqlite](https://hub.docker.com/r/upcyan/aircat-server-sqlite) | 斐讯悟空 M1 数据采集服务器（SQLite + Web 界面） |

- **支持架构**：amd64 / arm64
- **自动构建**：每次推送到 main 分支自动构建两个镜像并递增版本号

## 端口说明

| 端口 | 版本 | 说明 |
|------|------|------|
| 9000 | 通用 | TCP Socket 服务端口，接收 M1 设备连接 |
| 8080 | SQLite 版 | Web 界面端口，浏览器访问查看数据 |

## 许可证

MIT License
