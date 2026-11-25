# TCC — Benchmark de rede P2P

Ferramenta que mede a qualidade da conexão entre dois computadores quaisquer na
internet, sem servidor no meio. Dois peers se encontram sozinhos pela rede do
IPFS, abrem uma conexão WebRTC direta e medem latência, jitter, vazão, latência
sob carga e perda de pacotes. Depois de cada rodada, o peer roda também o
Speedtest da Ookla, como referência de comparação. Os resultados vão para
arquivos CSV e para o InfluxDB, e são visualizados no Grafana.

---

## Como funciona

| Container | Papel |
|---|---|
| `peer` | o benchmark em si. Leva dentro dele o Kubo (IPFS), usado para descoberta e sinalização, e o CLI do Speedtest |
| `influxdb` | guarda as medições como série temporal |
| `grafana` | mostra os gráficos, com dois dashboards já prontos |
| `dashboard-downloader` | só no `tcc-polics.yml`: baixa os dashboards do GitHub e encerra |

O ciclo de uma rodada:

1. **Descoberta.** Cada peer anuncia na DHT do IPFS uma chave derivada do tópico
   (`tcc-polics`) e da hora atual, procura quem anunciou a mesma chave e se
   conecta a esses peers.
2. **Anúncio.** A cada 2 segundos o peer publica no tópico seu status e seu
   **ASN**, o número que identifica o provedor de internet dele.
3. **Pareamento.** Ao ver outro peer livre, escolhe um parceiro, de preferência
   de **ASN diferente**; só cai para o mesmo ASN se não houver outra opção. O
   peer de menor PeerID faz o pedido e vira o cliente; o outro vira o servidor.
4. **Conexão.** Os dois trocam oferta e resposta WebRTC pelo próprio tópico e
   abrem a conexão direta, com cinco canais de dados: controle, latência, vazão,
   perda de pacotes e heartbeat.
5. **Testes.** Rodam em sequência: latência, vazão em quatro tamanhos (com
   latência sob carga) e perda de pacotes.
6. **Gravação.** Cada peer grava a sua linha no `results.csv` e no InfluxDB.
7. **Speedtest.** O peer espera 10 segundos e roda uma medição do Speedtest, que
   vai para o `speedtest.csv` e para a série `speedtest_results`.
8. **Intervalo.** O peer fica 150 segundos sem testar, volta a ficar livre e
   procura um novo par.

Se a conexão cair no meio, a rodada é gravada com `status = aborted`, com o que
já tinha sido medido, e o peer tenta parear de novo em 50 segundos.

**São necessárias pelo menos duas máquinas rodando a ferramenta.** Um peer
sozinho fica anunciando indefinidamente sem nada para medir.

---

## O que é medido

| Métrica | Como | Colunas no `results.csv` |
|---|---|---|
| Latência e jitter | 20 sondas de ida e volta, com o link ocioso | `latency (ms)`, `jitter (ms)` |
| Vazão | envio de 100 KB, 1 MB, 10 MB e 50 MB em cada sentido | `<tamanho>_upload (Mbps)`, `<tamanho>_download (Mbps)` |
| Latência sob carga | 20 sondas durante o upload do cliente, em cada tamanho | `<tamanho>_loaded_latency (ms)`, `<tamanho>_loaded_jitter (ms)` |
| Perda sob carga | pacotes esperados menos os recebidos em cada download | `<tamanho>_package_loss (%)` |
| Perda de pacotes | 1000 pacotes em lotes espaçados, com o link ocioso | `package_loss (%)` |

Cada linha traz ainda `timestamp` (UTC), `role` (cliente ou servidor), `peer_id`
e `remote_peer_id`, `ip`, o tipo de candidato ICE de cada ponta
(`candidate_type`, `remote_candidate_type`), `via_turn` e `status`.

O `speedtest.csv` tem `download`, `upload`, `latency`, `jitter`, latência sob
carga no download e no upload, `package_loss`, o provedor, o servidor usado pela
Ookla e o link do resultado.

---

## Pré-requisitos

- **Docker com o plugin do Compose**, em Linux, Windows (com WSL) ou macOS. O
  peer usa a rede do host (`network_mode: host`).
- **Portas livres:** 4001 TCP/UDP (IPFS), 5001 e 8080 (API e gateway do Kubo),
  8086 (InfluxDB) e 3000 (Grafana). Se o IPFS Desktop ou outro nó IPFS estiver
  aberto na máquina, feche antes de subir.

Confira a instalação:

```bash
docker compose version
```

---

## Rodando um peer

Não é preciso clonar o repositório. Basta o arquivo `tcc-polics.yml`, que usa a
imagem publicada no Docker Hub (`polics/tcc-polics`, para amd64 e arm64):

```bash
mkdir tcc && cd tcc
curl -O https://raw.githubusercontent.com/PolianaCSousa/TCC/main/tcc-polics.yml
docker compose -f tcc-polics.yml up -d
```

As credenciais do InfluxDB e do Grafana já vêm definidas no próprio arquivo; não
há nada para configurar.

Acompanhe:

```bash
docker compose -f tcc-polics.yml ps            # influxdb deve ficar "healthy"
docker compose -f tcc-polics.yml logs -f peer
```

Nos primeiros minutos é normal ver o peer só anunciando: a descoberta pela DHT
pode levar alguns minutos.

Para atualizar, rode o `up -d` de novo. Ele sempre baixa a imagem mais recente;
um `docker restart` não baixa.

---

## Vendo os resultados

**Grafana:** <http://localhost:3000>. Usuário `admin`; a senha é o valor de
`GF_SECURITY_ADMIN_PASSWORD` no `tcc-polics.yml`. A fonte de dados e dois
dashboards já vêm configurados:

- *TCC — Benchmark P2P WebRTC*: vazão, latência, latência sob carga e perda de
  pacotes por rodada e por tamanho de teste;
- *TCC — Speedtest (Ookla)*: as medições do Speedtest e a comparação com a
  ferramenta.

**InfluxDB:** <http://localhost:8086>, acessível só da própria máquina. Usuário
e senha estão em `DOCKER_INFLUXDB_INIT_USERNAME` e
`DOCKER_INFLUXDB_INIT_PASSWORD`. As séries são `tests_results` e
`speedtest_results`, no bucket `internet_metrics`.

**CSV:** `data/peer/results.csv` e `data/peer/speedtest.csv`, uma linha por
rodada. São a fonte principal: se a escrita no banco falhar, o teste continua e
o CSV é gravado do mesmo jeito.

---

## Onde os dados ficam

| O quê | Onde | Sobrevive a `docker compose down`? |
|---|---|---|
| `results.csv`, `speedtest.csv` e `logs/` | `data/peer/`, ao lado do `tcc-polics.yml` | sim |
| Banco do Influx | volumes `influx-data` e `influx-config` | sim |
| Dados do Grafana | volume `grafana-data` | sim |
| Dashboards e provisionamento | `grafana/`, baixados do GitHub a cada `up` | sim |

- **`docker compose down -v` apaga os volumes**, e o banco vai junto. A pasta
  `data/` não é afetada.
- **No Linux, os arquivos saem pertencendo ao root**, porque os containers
  rodam como root. Para editar ou apagar: `sudo chown -R $USER: data grafana`.
- Se as colunas mudarem entre versões, o CSV antigo é renomeado para
  `results_AAAAMMDD-HHMMSS.csv` e um novo é iniciado.
- O log (`data/peer/logs/peer_5001.log`) é sobrescrito a cada reinício do peer.
- Não há backup: copie a pasta `data/` para um lugar seguro.

---

## Comandos do dia a dia

```bash
docker compose -f tcc-polics.yml ps              # o que está no ar
docker compose -f tcc-polics.yml logs -f peer    # acompanhar o benchmark
docker compose -f tcc-polics.yml restart peer    # reiniciar só o peer
docker compose -f tcc-polics.yml down            # desligar tudo, preservando os dados
docker compose -f tcc-polics.yml up -d           # subir de novo, com a imagem mais recente
```

---

## Problemas comuns

| Sintoma no log | Causa provável | O que fazer |
|---|---|---|
| `Sem par há 60s` repetido logo após subir | a descoberta ainda não terminou, ou não há outro peer no ar | esperar alguns minutos; confirmar que há outra máquina rodando |
| `experimental pubsub feature not enabled` | outro nó IPFS (ex.: IPFS Desktop) ocupa as portas 4001/5001 e o peer está falando com ele | fechar o outro IPFS e subir de novo |
| Os peers se veem no tópico, mas a conexão nunca sobe | NAT restritivo nas duas pontas | ver [Limitações conhecidas](#limitações-conhecidas) |
| `Rodada abortada` | a conexão caiu ou travou durante os testes | nada: a rodada parcial é gravada e o peer repareia sozinho |

---

## Desenvolvimento

### Estrutura do código

| Arquivo | Responsabilidade |
|---|---|
| `peer.py` | ponto de entrada: ciclo de rodadas, conexão WebRTC e canais de dados |
| `ipfs_signaling.py` | anúncio, pareamento e troca de oferta/resposta pelo pubsub do IPFS |
| `swarm_connector.py` | descoberta dos outros peers pela DHT |
| `kubo_client.py` | cliente da API HTTP do Kubo |
| `experiments/latency.py` | teste de latência |
| `experiments/throughput.py` | teste de vazão, controle de fluxo e detecção de travamento |
| `speedtest_runner.py` | execução do Speedtest da Ookla |
| `storage.py`, `influx_service.py` | gravação em CSV e no InfluxDB |
| `state.py`, `custom_types.py` | estado da rodada e tipos |
| `constants.py`, `config.py` | tamanhos de teste, timeouts e configuração de ICE |
| `isp.py` | descoberta do IP público e do ASN |
| `utils.py` | envio seguro e esperas com timeout |
| `rendezvous.py` | servidor de sinalização antigo (Socket.IO); não é mais usado |

### Construindo a imagem

O `Dockerfile` não copia o código do disco: ele clona a branch `speedtest-cli`
do GitHub. Da pasta local só entram o `requirements.txt` e o `.env`. Para uma
mudança de código chegar à imagem:

1. faça commit e push na branch `speedtest-cli`;
2. reconstrua **sem cache**, senão a camada do `git clone` é reaproveitada e a
   imagem sai com o código antigo:

   ```bash
   docker buildx build --builder multiarch-builder \
     --platform linux/amd64,linux/arm64 \
     --no-cache -t polics/tcc-polics:latest --push .
   ```

3. nas máquinas que rodam o peer, `docker compose -f tcc-polics.yml up -d`.

O `docker-compose.yml` não faz parte do fluxo atual: a execução é sempre pelo
`tcc-polics.yml`.

### Rodando fora do Docker

Com o Kubo e o CLI do Speedtest instalados na máquina:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

ipfs init
ipfs config --json Pubsub.Enabled true
ipfs daemon &

python peer.py
```

Os arquivos `results.csv`, `speedtest.csv` e `logs/` são criados na pasta de
onde o comando foi executado. Sem o InfluxDB no ar, o peer avisa no log e segue
gravando só nos CSV.

### Ajustando o Grafana

Exporte o painel como JSON e salve em `grafana/dashboards/`. Para chegar a quem
usa o `tcc-polics.yml`, o arquivo precisa estar na branch `speedtest-cli` do
GitHub, de onde a pasta `grafana/` é baixada a cada `up`; alterações feitas só
na pasta local são sobrescritas.

---

## Limitações conhecidas

- **Sem servidor de relay (TURN).** A ferramenta usa apenas o STUN público do
  Google. Se os dois peers estiverem atrás de NAT restritivo, a conexão direta
  não se estabelece e o peer volta a procurar par.
- **Um peer por máquina.** O peer ocupa a porta 4001 do host e a pasta
  `data/peer`; duas instâncias na mesma máquina entrariam em conflito.
- **Sem backup.** Os CSV existem em um único lugar.
