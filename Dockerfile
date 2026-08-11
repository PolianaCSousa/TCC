FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# requirements primeiro: só reinstala as libs quando esse arquivo muda,
# não a cada alteração no .py
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY experiments ./experiments

# usuário não-root com o mesmo UID/GID do dono de ./data no host,
# senão o results.csv sai pertencendo ao root e você não consegue editar
RUN groupadd -g ${APP_GID} app \
 && useradd -u ${APP_UID} -g ${APP_GID} -m app \
 && mkdir -p /data \
 && chown ${APP_UID}:${APP_GID} /data
USER app

# storage.py grava results.csv e logs/ em caminho relativo, então o CWD precisa
# ser o volume montado. O código continua em /app: como o peer.py está lá, o
# Python resolve os imports a partir de /app mesmo com o CWD em /data.
WORKDIR /data
CMD ["python", "/app/peer.py"]
