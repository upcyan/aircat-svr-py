FROM python:3.14-slim


WORKDIR /aircat-server


ADD aircat-server-lite.py .
ADD aircat-server-py/common/function.py ./common/

# 安装 pipreqs 并生成依赖，然后安装依赖

RUN pip install pipreqs -i https://mirrors.ustc.edu.cn/pypi/simple && \

    pipreqs ./ --encoding=utf-8 && \

    pip install -r requirements.txt -i https://mirrors.ustc.edu.cn/pypi/simple


CMD [ "python", "aircat-server-lite.py" ]
