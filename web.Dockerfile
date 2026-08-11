# ===== 阶段1：下载 echarts（不进入最终镜像）=====
FROM python:3.14-slim AS echarts-stage
RUN mkdir -p /static && \
    python -c "import urllib.request; urllib.request.urlretrieve('https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js', '/static/echarts.min.js')" && \
    test -s /static/echarts.min.js

# ===== 阶段2：最终镜像 =====
FROM python:3.14-slim

# 安全更新 + 安装依赖 + 清理（合并为单层减小体积）
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends tzdata && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir --no-compile duckdb pytz && \
    find /usr/local/lib/python3.14 -type d \( -name "test*" -o -name "__pycache__" \) -exec rm -rf {} + 2>/dev/null; \
    rm -rf /usr/local/lib/python3.14/idlelib /usr/local/lib/python3.14/lib2to3 2>/dev/null; \
    true

WORKDIR /aircat-server

COPY --from=echarts-stage /static ./static

COPY aircat-server-web.py .
COPY storage_backends.py .
COPY aircat-server-py/templates/web.html ./aircat-server-py/templates/
COPY VERSION .

RUN echo "Building aircat-server-web v$(cat VERSION)" && mkdir -p /data

VOLUME ["/data"]

ENTRYPOINT ["python", "aircat-server-web.py"]
