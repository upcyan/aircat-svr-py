FROM python:3.14-alpine

RUN apk add --no-cache tzdata

WORKDIR /aircat-server

COPY VERSION .

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
LABEL org.opencontainers.image.title="aircat-server-lite" \
      org.opencontainers.image.description="Phicomm M1 air quality monitor server (lite version)" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="https://github.com/upcyan/aircat-svr-py" \
      org.opencontainers.image.licenses="MIT"

COPY aircat-server-lite.py .
COPY server_common.py .

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import socket; s=socket.create_connection(('127.0.0.1',9000),2); s.close()" || exit 1

CMD [ "python", "aircat-server-lite.py" ]
