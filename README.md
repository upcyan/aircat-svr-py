# 斐讯空气猫 M1 服务器

> Docker 镜像：[upcyan/aircat-server-lite](https://hub.docker.com/r/upcyan/aircat-server-lite)
>
> 支持架构：`linux/amd64` · `linux/arm64`

基于 Docker 和 Python 的斐讯空气猫（Phicomm AirCat）M1 设备数据采集服务器。

## 功能特性

- 监听 TCP Socket 端口，接收 M1 设备上报的环境数据
- 解析设备数据：湿度、温度、PM2.5、甲醛（HCHO）
- 完善的日志记录功能（控制台 + 文件）
- Docker 容器化部署，一键启动
- 支持多客户端并发连接
- 自动断线重连机制
- 多架构镜像支持（x86 / ARM64）

## 技术栈

- **语言**: Python 3.14
- **框架**: 原生 Socket
- **容器**: Docker / Docker Compose
- **CI/CD**: GitHub Actions 自动构建

## 项目结构

```
aircat-svr-py/
├── aircat-server-lite.py      # 轻量级服务器主程序
├── aircat-server-py/
│   ├── common/
│   │   ├── function.py        # 工具函数
│   │   └── sql.conf           # 配置文件
│   ├── static/                # 静态资源（CSS、JS、图片）
│   └── templates/             # HTML 模板
├── docker-yaml/
│   └── docker-lite/
│       └── docker-compose.yml  # Docker Compose 配置
├── lite.Dockerfile            # Docker 镜像构建文件
├── VERSION                    # 版本号
└── README.md                  # 项目说明文档
```

## 快速开始

### 方式一：使用 Docker 镜像（推荐）

直接从 Docker Hub 拉取预构建镜像，无需本地构建。

```bash
# 拉取最新镜像
docker pull upcyan/aircat-server-lite:latest

# 运行容器
docker run -d \
  --name aircat-server \
  -p 9000:9000 \
  -e TZ=Asia/Shanghai \
  --restart always \
  upcyan/aircat-server-lite:latest

# 查看日志
docker logs -f aircat-server
```

支持的镜像标签：

| 标签 | 说明 |
|------|------|
| `latest` | 最新稳定版 |
| `0.1.1` | 指定版本号 |

> ARM 设备（树莓派、群晖 ARM 等）会自动拉取 arm64 架构镜像，无需额外配置。

### 方式二：使用 Docker Compose

创建 `docker-compose.yml` 文件：

```yaml
version: '3.8'
services:
  aircat-server:
    image: upcyan/aircat-server-lite:latest
    container_name: aircat-server
    ports:
      - "9000:9000"
    environment:
      - TZ=Asia/Shanghai
    restart: always
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

启动服务：

```bash
# 启动
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止
docker-compose down
```

### 方式三：本地构建 Docker 镜像

```bash
# 构建镜像
docker build -t aircat-server-lite -f lite.Dockerfile .

# 运行容器
docker run -d -p 9000:9000 --name aircat-server-lite aircat-server-lite

# 查看日志
docker logs -f aircat-server-lite
```

### 方式四：直接运行 Python

```bash
# 安装依赖
pip install -r requirements.txt

# 运行服务器
python aircat-server-lite.py
```

## 配置说明

服务器默认配置如下：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 监听端口 | 9000 | TCP Socket 端口 |
| 采集间隔 | 5 秒 | 设备数据采集频率 |
| 接收缓冲区 | 4096 字节 | Socket 接收缓冲区大小 |
| 接收超时 | 30 秒 | 数据接收超时时间 |
| 最大重试次数 | 5 次 | 超时后最大重试次数 |
| 日志目录 | `logs/` | 日志文件存储目录 |

## 数据格式

服务器接收 M1 设备上报的 JSON 数据，包含以下字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| humidity | float | 湿度（%） |
| temperature | float | 温度（°C） |
| value | int | PM2.5 值 |
| hcho | float | 甲醛浓度（mg/m³） |

## 日志输出示例

```
2024-01-15 10:30:00,123 - PhicommM1 Server - INFO : Server started on 0.0.0.0:9000
2024-01-15 10:30:05,456 - PhicommM1 Server - INFO : New connection from ('192.168.1.100', 12345)
2024-01-15 10:30:10,789 - PhicommM1 Server - DEBUG : Data from ('192.168.1.100', 12345): H=45.2%, T=22.5°C, PM2.5=35, HCHO=0.03
```

## Docker Hub 仓库

镜像地址：[hub.docker.com/r/upcyan/aircat-server-lite](https://hub.docker.com/r/upcyan/aircat-server-lite)

- **仓库名**：`upcyan/aircat-server-lite`
- **中文简介**：斐讯空气猫 M1 数据采集服务器
- **支持架构**：amd64 / arm64
- **自动构建**：每次推送到 main 分支自动构建并递增版本号

## 端口说明

- **9000**: TCP Socket 服务端口，用于接收 M1 设备连接

## 许可证

MIT License
