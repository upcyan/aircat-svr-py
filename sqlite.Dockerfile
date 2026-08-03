FROM python:3.14-slim

WORKDIR /aircat-server

COPY aircat-server-sqlite.py .
COPY aircat-server-py/templates/sqlite.html ./aircat-server-py/templates/
COPY aircat-server-py/static/ ./aircat-server-py/static/

RUN mkdir -p /data

VOLUME ["/data"]

CMD [ "python", "aircat-server-sqlite.py" ]
