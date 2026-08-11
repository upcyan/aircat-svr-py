FROM python:3.14-slim

# 安装安全更新（修复 Debian 基础镜像中的已知 CVE）+ 必要依赖
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends tzdata && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /aircat-server

# 安装依赖：duckdb（可选存储引擎）+ pytz（duckdb 时区支持）
RUN pip install --no-cache-dir duckdb pytz

COPY aircat-server-web.py .
COPY storage_backends.py .
COPY aircat-server-py/templates/web.html ./aircat-server-py/templates/
COPY VERSION .
RUN echo "Building aircat-server-web v$(cat VERSION)"

# 下载 echarts 到本地（构建时种子文件，启动时若有新版本会自动覆盖）
RUN mkdir -p static && \
    python -c "import urllib.request; urllib.request.urlretrieve('https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js', 'static/echarts.min.js')" && \
    test -s static/echarts.min.js && echo "echarts downloaded OK"

RUN mkdir -p /data

VOLUME ["/data"]

ENTRYPOINT ["python", "aircat-server-web.py"]
