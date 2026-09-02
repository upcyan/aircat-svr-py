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

CMD [ "python", "aircat-server-lite.py" ]
