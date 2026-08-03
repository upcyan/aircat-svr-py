FROM python:3.14-slim

WORKDIR /aircat-server

COPY aircat-server-sqlite.py .
COPY aircat-server-py/templates/sqlite.html ./aircat-server-py/templates/

RUN mkdir -p /data

VOLUME ["/data"]

ENTRYPOINT ["python", "aircat-server-sqlite.py"]
