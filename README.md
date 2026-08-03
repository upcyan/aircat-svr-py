# 斐讯悟空 M1 服务器

> **Lite 版**：[upcyan/aircat-server-lite](https://hub.docker.com/r/upcyan/aircat-server-lite) — 轻量级数据采集
>
> **SQLite 版**：[upcyan/aircat-server-sqlite](https://hub.docker.com/r/upcyan/aircat-server-sqlite) — 数据存储 + Web 可视化界面
>
> 支持架构：`linux/amd64` · `linux/arm64`

> 本项目基于 [fenggenet/PhicommM1_Server](https://github.com/fenggenet/PhicommM1_Server) 修改而来，在原项目基础上增加了 Docker 容器化部署、SQLite 存储、Web 可视化界面、设备亮度控制等功能。

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
- M1 设备屏幕亮度控制（固定亮度 / 定时开关屏）

### SQLite 版独有功能
- 数据自动存入 SQLite 数据库，支持持久化
- 内置 Web 管理界面，实时查看各项数据
- 历史数据折线图（ECharts），支持点击图例隐藏/显示各项数据
- 时间范围切换：1小时 / 6小时 / 24小时 / 7天
- 实时数据自动刷新（5秒更新卡片，60秒更新图表）
- 数据量限制与自动清理（可配置最大记录数和保存天数）
- Web 设置面板（认证、数据管理、调试设置）
- 可选用户名密码认证（默认不启用）
- 支持 Docker 命令行重置用户名密码

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

# 运行容器（默认不启用认证）
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

# 查看日志
docker logs -f aircat-server-sqlite
```

首次启动时通过环境变量配置认证（可选）：

```bash
docker run -d \
  --name aircat-server-sqlite \
  -p 9000:9000 \
  -p 8080:8080 \
  -e TZ=Asia/Shanghai \
  -e AUTH_USER=admin \
  -e AUTH_PASS=yourpassword \
  -v ./data:/data \
  --restart always \
  upcyan/aircat-server-sqlite:latest
```

#### 重置用户名和密码

```bash
# 重置用户名
docker exec -it aircat-server-sqlite resetname

# 重置密码
docker exec -it aircat-server-sqlite resetpasswd
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
      # 可选: 首次启动设置用户名（留空则不启用认证）
      # - AUTH_USER=admin
      # 可选: 首次启动设置密码（设置后自动启用认证）
      # - AUTH_PASS=yourpassword
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

#### Web 设置面板

点击页面右上角齿轮图标打开设置面板，支持：

- **认证设置**：启用/关闭登录认证，设置用户名和密码
- **数据管理**：设置最大记录数（超出自动覆盖）、保存天数（超期自动清理）、手动清理全部数据
- **调试设置**：切换日志级别（DEBUG/INFO/WARNING/ERROR）、开启/关闭日志文件
- **设备控制**：设置 M1 屏幕亮度（不控制/息屏/微亮/较暗/较亮/正常）、定时开关屏（白天/夜晚亮度和时间）

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

### M1 设备亮度控制环境变量

| 环境变量 | 默认值 | 可选值 | 说明 |
|----------|--------|--------|------|
| `M1_BRIGHTNESS` | `-1` | `-1` / `0` / `25` / `50` / `75` / `100` | 屏幕亮度，-1=不控制，0=息屏，100=最亮 |
| `M1_TIMER_ENABLED` | `false` | `true` / `false` | 启用定时开关屏 |
| `M1_TIMER_DAY_BRIGHTNESS` | `100` | `0` / `25` / `50` / `75` / `100` | 白天屏幕亮度 |
| `M1_TIMER_NIGHT_BRIGHTNESS` | `0` | `0` / `25` / `50` / `75` / `100` | 夜晚屏幕亮度 |
| `M1_TIMER_DAY_START` | `07:00` | `HH:MM` | 白天开始时间 |
| `M1_TIMER_NIGHT_START` | `23:00` | `HH:MM` | 夜晚开始时间 |

> `M1_BRIGHTNESS` 优先级高于定时设置。当 `M1_BRIGHTNESS >= 0` 时使用固定亮度，忽略定时设置。
>
> Lite 版通过环境变量配置，SQLite 版通过 Web 设置面板配置（也可通过环境变量初始化）。

### SQLite 版独有环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `DB_PATH` | `/data/aircat.db` | SQLite 数据库文件路径 |
| `WEB_PORT` | `8080` | Web 界面端口 |
| `AUTH_USER` | （空） | 首次启动设置用户名，留空则不启用认证 |
| `AUTH_PASS` | （空） | 首次启动设置密码，设置后自动启用认证 |

> `AUTH_USER` 和 `AUTH_PASS` 仅在首次启动时写入数据库，后续修改请通过 Web 设置面板或 `docker exec` 命令。

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

| 接口 | 方法 | 认证 | 说明 |
|------|------|------|------|
| `/` | GET | 可选 | Web 界面页面 |
| `/api/latest` | GET | 可选 | 获取最新一条数据记录 |
| `/api/history?hours=24` | GET | 可选 | 获取指定小时数内的历史数据 |
| `/api/settings` | GET | 需要 | 获取当前设置 |
| `/api/settings` | POST | 需要 | 更新设置（最大记录数、保存天数、认证、日志等） |
| `/api/cleanup` | POST | 需要 | 清理全部传感器数据 |
| `/api/login` | POST | 不需要 | 登录认证，返回 token |

### 数据管理配置（SQLite 版）

通过 Web 设置面板或 API 配置，所有设置持久化在 SQLite 数据库中：

| 设置项 | 默认值 | 说明 |
|--------|--------|------|
| `max_records` | 10000 | 最大记录数，超出后自动删除最早记录 |
| `retention_days` | 30 | 保存天数，超期记录自动清理 |
| `auth_enabled` | 0 | 是否启用登录认证（0=关闭，1=开启） |
| `auth_user` | （空） | 登录用户名 |
| `log_level` | `DEBUG` | 日志级别（可通过 Web 面板动态修改） |
| `log_file` | 0 | 是否写入日志文件（0=关闭，1=开启） |

> 数据清理由后台线程每 5 分钟自动执行一次，同时每次插入数据后也会检查。

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
