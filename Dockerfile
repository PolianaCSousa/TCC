FROM python:3.12-slim


ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

SHELL ["/bin/bash","-c"]

RUN apt-get update
RUN apt-get install -y --no-install-recommends \
    git wget curl 
    #python3 python3-venv python3-pip 

    
# requirements primeiro: só reinstala as libs quando esse arquivo muda,
# não a cada alteração no .py
COPY requirements.txt /app
RUN cd /app && python3 -m venv venv && source venv/bin/activate
# Só se o `pip install` falhar compilando o `aiortc`:** normalmente ele instala por wheel pré-compilado e nada abaixo é necessário. Se der erro de compilação, instale as libs de mídia e tente de novo:
#RUN apt install -y --no-install-recommends build-essential python3-dev pkg-config libavdevice-dev libavfilter-dev libopus-dev libvpx-dev libsrtp2-dev
RUN pip install --upgrade pip ; pip install --no-cache-dir -r requirements.txt
    
#RUN wget https://dist.ipfs.tech/kubo/v0.43.0/kubo_v0.43.0_linux-amd64.tar.gz && \
RUN wget https://github.com/ipfs/kubo/releases/download/v0.43.0/kubo_v0.43.0_linux-amd64.tar.gz 
RUN tar -xvzf kubo_v0.43.0_linux-amd64.tar.gz && \
    cd kubo && sh install.sh && cd ..  && \
    ipfs --version 

RUN git clone -b dockerfile https://github.com/PolianaCSousa/TCC.git /TCC
RUN mv /TCC/* /app
# RUN cd /app && python3 -m venv venv && source venv/bin/activate
# RUN pip install --no-cache-dir -r requirements.txt

RUN chmod +x /app/entrypoint.sh

RUN mkdir -p /data

# storage.py grava results.csv e logs/ em caminho relativo, então o CWD precisa
# ser o volume montado. O código continua em /app: como o peer.py está lá, o
# Python resolve os imports a partir de /app mesmo com o CWD em /data.
WORKDIR /data

COPY .env /app
# The script handles launching the background service
ENTRYPOINT ["/app/entrypoint.sh"] 

#atualmente o estado atual é: instalar o ipfs dentro do proprio container do peer. Removemos o serviço
#do ipfs do docker-compose, mas pode ser que alguma coisa esteja faltando. Verificar.