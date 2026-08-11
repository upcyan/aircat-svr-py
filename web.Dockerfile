# ===== 阶段1：下载 echarts（不进入最终镜像）=====
FROM python:3.14-slim AS echarts-stage
RUN mkdir -p /static && \
    python -c "import urllib.request; urllib.request.urlretrieve('https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js', '/static/echarts.min.js')" && \
    test -s /static/echarts.min.js

# ===== 阶段2：安装 pip 依赖（不进入最终镜像）=====
FROM python:3.14-slim AS deps-stage
RUN pip install --no-cache-dir --no-compile --no-deps --target=/pylibs duckdb pytz && \
    find /pylibs -name "*.so" -exec strip --strip-unneeded {} + 2>/dev/null; \
    find /pylibs -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; \
    true

# ===== 阶段3：最终镜像 =====
FROM python:3.14-slim

# 安全更新 + 安装依赖 + 深度清理（合并为单层）
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends tzdata && \
    apt-get purge -y --auto-remove && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/* && \
    # 删除不需要的 Python 标准库模块
    find /usr/local/lib/python3.14 -type d \( \
      -name "test*" -o -name "__pycache__" -o -name "idlelib" -o \
      -name "lib2to3" -o -name "tkinter" -o -name "turtle*" -o \
      -name "ensurepip" -o -name "venv" \
    \) -exec rm -rf {} + 2>/dev/null; \
    rm -rf /usr/local/lib/python3.14/config* 2>/dev/null; \
    rm -rf /usr/local/include 2>/dev/null; \
    true

WORKDIR /aircat-server

# 从 deps 阶段复制已 strip 的依赖库
COPY --from=deps-stage /pylibs /usr/local/lib/python3.14/site-packages/
COPY --from=echarts-stage /static ./static

COPY VERSION .

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

COPY aircat-server-web.py .
COPY storage_backends.py .
COPY aircat-server-py/templates/web.html ./aircat-server-py/templates/

RUN echo "Building aircat-server-web v$(cat VERSION)" && mkdir -p /data

VOLUME ["/data"]

ENTRYPOINT ["python", "aircat-server-web.py"]
