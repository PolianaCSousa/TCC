FROM python:3.12-slim


ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# requirements primeiro: só reinstala as libs quando esse arquivo muda,
# não a cada alteração no .py
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt update ; apt install wget
RUN wget https://dist.ipfs.tech/kubo/v0.42.0/kubo_v0.42.0_linux-amd64.tar.gz
RUN tar -xvzf kubo_v0.42.0_linux-amd64.tar.gz
RUN cd kubo && sh install.sh && cd .. 
RUN ipfs --version 
RUN ipfs init 
RUN ipfs config --json Pubsub.Enabled true

COPY *.py /app/
COPY experiments /app/experiments

COPY run-ipfs-and-peer.sh /app/run-ipfs-and-peer.sh
RUN chmod +x /app/run-ipfs-and-peer.sh

RUN mkdir -p /data

# storage.py grava results.csv e logs/ em caminho relativo, então o CWD precisa
# ser o volume montado. O código continua em /app: como o peer.py está lá, o
# Python resolve os imports a partir de /app mesmo com o CWD em /data.
WORKDIR /data
# CMD ["python", "/app/peer.py"]

# The script handles launching the background service
ENTRYPOINT ["/app/run-ipfs-and-peer.sh"]