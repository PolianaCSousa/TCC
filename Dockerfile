FROM python:3.12-slim


ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

SHELL ["/bin/bash","-c"]

RUN apt-get update
RUN apt-get install -y --no-install-recommends \
    git wget curl 

    
# requirements before the code: makes use of the layer cache
COPY requirements.txt /app
RUN cd /app && python3 -m venv venv && source venv/bin/activate
RUN pip install --upgrade pip ; pip install --no-cache-dir -r requirements.txt
    
# buildx sets TARGETARCH to amd64 or arm64
ARG TARGETARCH

RUN wget https://github.com/ipfs/kubo/releases/download/v0.43.0/kubo_v0.43.0_linux-${TARGETARCH}.tar.gz
RUN tar -xvzf kubo_v0.43.0_linux-${TARGETARCH}.tar.gz && \
    cd kubo && sh install.sh && cd ..  && \
    ipfs --version

# Ookla Speedtest CLI: comparison baseline
RUN case "${TARGETARCH}" in \
        amd64) SPEEDTEST_ARCH=x86_64 ;; \
        arm64) SPEEDTEST_ARCH=aarch64 ;; \
        *) echo "arquitetura sem speedtest: ${TARGETARCH}"; exit 1 ;; \
    esac && \
    wget https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-${SPEEDTEST_ARCH}.tgz && \
    tar -xzf ookla-speedtest-1.2.0-linux-${SPEEDTEST_ARCH}.tgz -C /usr/local/bin speedtest && \
    rm ookla-speedtest-1.2.0-linux-${SPEEDTEST_ARCH}.tgz && \
    speedtest --version

RUN git clone -b speedtest-cli https://github.com/PolianaCSousa/TCC.git /TCC
RUN mv /TCC/* /app

RUN chmod +x /app/entrypoint.sh

RUN mkdir -p /data

# CWD on the volume: results.csv and logs/ are written to relative paths
WORKDIR /data

COPY .env /app
ENTRYPOINT ["/app/entrypoint.sh"] 