FROM python:3.14-alpine

RUN apk add --no-cache tzdata

WORKDIR /aircat-server

COPY VERSION .

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

COPY aircat-server-lite.py .

CMD [ "python", "aircat-server-lite.py" ]
