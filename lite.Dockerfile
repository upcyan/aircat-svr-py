FROM python:3.14-slim

WORKDIR /aircat-server

COPY aircat-server-lite.py .

CMD [ "python", "aircat-server-lite.py" ]
