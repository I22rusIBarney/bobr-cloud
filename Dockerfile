FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt
COPY . .
# бот работает не от root: данные — в смонтированной data/, её владельца bcloud выставляет при каждом запуске (fix_perms в install.sh)
RUN python -m compileall -q app main.py && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bcloud
USER 10001
CMD ["python", "main.py"]
