FROM python:3.14-alpine

RUN apk add --no-cache tzdata

WORKDIR /aircat-server

COPY aircat-server-sqlite.py .
COPY aircat-server-py/templates/sqlite.html ./aircat-server-py/templates/

# 下载 echarts 到本地，避免运行时依赖外部 CDN（国内访问 jsdelivr 经常超时）
RUN mkdir -p static && \
    python -c "import urllib.request; urllib.request.urlretrieve('https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js', 'static/echarts.min.js')" && \
    test -s static/echarts.min.js && echo "echarts downloaded OK"

RUN mkdir -p /data

VOLUME ["/data"]

ENTRYPOINT ["python", "aircat-server-sqlite.py"]
