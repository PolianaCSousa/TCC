# --- peer status ---
FREE = "FREE"
OCCUPIED = "OCCUPIED"
PAIRING = "PAIRING"
PAIRED = "PAIRED"

# --- peer roles ---
CLIENT = "client"
SERVER = "server"

# --- label for channels ---
CONTROL = "control"
LATENCY = "latency"
THROUGHPUT = "throughput"
PACKAGE_LOSS = "package_loss"
HEARTBEAT = "heartbeat"

# --- flags for acks and end of tests ---
END_LATENCY = "Fim latência"
START_THROUGHPUT = "Os testes de vazão irão começar"
END_THROUGHPUT = "fim"
UPLOAD_RECEIVED = "upload_received"
LAT_ACK_ERROR = "Erro na latência. O pacote LAT_ACK não foi entregue."
UPLOAD_ERROR = "Upload não foi recebido"
# o remetente desistiu do envio no meio: sem este aviso o receptor fica esperando o
# END_THROUGHPUT que nunca vem, e gasta test_size/MIN_THROUGHPUT (800s no 100MB) à toa
SEND_ABORTED = "envio abortado pelo remetente"
END_TEST = "Teste finalizado"
LAT = "LAT"
LAT_ACK = "LAT-ACK"
ACK = "ACK"
LOADED_LATENCY = "loaded_latency"
END_ITERATION = "fim da iteração"
END_LAT_PACKAGES = "cliente terminou o envio dos pacotes do teste de latência"
START_LOADED_PACKAGES = "cliente vai começar o envio dos pacotes do teste de latência carregada"
END_LOADED_PACKAGES = "cliente terminou o envio dos pacotes do teste de latência carregada"
END_PACKAGE_LOSS = "fim do envio dos pacotes para cálculo da perda de pacotes"
ACK_PACKAGE_LOSS = "valor (%) da perda de pacotes recebido"
PACKAGE_LOSS_TIMEOUT = 5
LATENCY_TEST_SIZE = 20

# --- throughput test size ---
BYTES_THROUGHPUT_100KB = 100 * 10 ** 3
BYTES_THROUGHPUT_1MB = 1 * 10 ** 6
BYTES_THROUGHPUT_10MB = 10 * 10 ** 6
BYTES_THROUGHPUT_100MB = 100 * 10 ** 6
THROUGHPUT_LABELS = {
    BYTES_THROUGHPUT_100KB: "100KB",
    BYTES_THROUGHPUT_1MB: "1MB",
    BYTES_THROUGHPUT_10MB: "10MB",
    BYTES_THROUGHPUT_100MB: "100MB",
}

# --- status da rodada (vira coluna no results.csv e tag no influx) ---
COMPLETE = "complete"
ABORTED = "aborted"

# limite mínimo do buffer para controle de fluxo - eu so envio mais quando ele tiver abaixo desse valor
BUFFER_AMOUNT_LIMIT = {
    BYTES_THROUGHPUT_100KB: 20 * 10 ** 3,
    BYTES_THROUGHPUT_1MB: 100 * 10 ** 3,
    BYTES_THROUGHPUT_10MB: 1 * 10 ** 6,
    BYTES_THROUGHPUT_100MB: 1 * 10 ** 6,
}

# menor vazão que eu espero que meus usuários tenham
__MIN_THROUGHPUT_MbitPerSec = 1  ## 1 MbitPerSec - essa variavel é a velocidade contratada pelo usuário mais humilde
MIN_THROUGHPUT_BytePerSec = (__MIN_THROUGHPUT_MbitPerSec / 8) * 10 ** 6  ## 8 MbitPerSec / 8 bits = 1 MBytePerSec - aqui como eu estou enviando Bytes no meu teste de vazão, eu preciso converter de bit pra Byte e multiplicar pela potência de 10 do MB

# --- package size ---
BYTES_PER_PACKAGE = 1400

# --- heartbeat ---
HEARTBEAT_INTERVAL_SECONDS = 10
# payload cabe na MTU típica depois do overhead de SCTP/DTLS/UDP, então sai como um único datagrama
HEARTBEAT_PACKAGE_SIZE = 1200

# --- timeouts ---
_ACCEPTABLE_LATENCY_MS = 80
LATENCY_TIMEOUT = 10 * (_ACCEPTABLE_LATENCY_MS / 1000)  # estou considerando que a latencia aceitável é de 80ms. Meu timeout vai esperar até 10 vezes isso.
LOADED_LATENCY_TIMEOUT = 2 
SHORT_TIMEOUT = 12 * (_ACCEPTABLE_LATENCY_MS / 1000)  # criei esse timeout pra esperar os acks - MOSTRAR EVERTHON

# LATENCY_PROBE_INTERVAL = 0.001 # 10 ms
LATENCY_PROBE_INTERVAL = 0.002 # 20 ms

# --- controle de fluxo do teste de vazão ---
# O envio precisa ESPERAR o buffer drenar em vez de continuar enfileirando. Encher a
# fila do gargalo infla o RTT muito acima dos 0,5s que o consent freshness do ICE
# (RFC 7675) tolera, e o aioice derruba a conexão depois de 6 falhas seguidas (~30s).
# Foi isso que matou a conexão logo depois que o teste de 100MB começou.
BUFFER_DRAIN_TIMEOUT = 5         # quanto espero, por vez, o bufferedamountlow chegar
MAX_BUFFER_STALLS = 6            # 6 esperas seguidas sem drenar (~30s) = link travado, aborto o envio

# Detector por TAXA. O contador acima só pega o buffer LITERALMENTE parado: um
# gotejo de 1 pacote a cada ~10s dispara o evento de drenagem, zera o contador e
# passa por link saudável — 82 avisos "há 5s" em 13min em 2026-09-26 19:18, até
# ser morto na mão. Aqui a pergunta é outra: quantos bytes avançaram nos últimos
# STALL_WINDOW_SECONDS? Abaixo do piso, seja qual for o ritmo do evento, é SCTP
# travado e a rodada cai. O piso é uma fração do link mais lento que a ferramenta
# assume (MIN_THROUGHPUT): um link legítimo de 0,5Mbps passa com 5x de folga; o
# gotejo observado (~140 B/s) falha por ~90x.
STALL_WINDOW_SECONDS = 30
STALL_RATE_FRACTION = 0.1
STALL_FLOOR_BytePerSec = MIN_THROUGHPUT_BytePerSec * STALL_RATE_FRACTION   # 12.500 B/s

# --- autoajuste do envio por atraso (controle estilo Vegas/BBR, simplificado) ---
# O congestion control do SCTP reage a PERDA, e perda só existe depois que a fila do
# gargalo transbordou: por projeto ele ENCHE a fila. Em 2026-09-23 isso levou a
# loaded_latency a 1451ms (contra os 500ms que o consent do ICE tolera) e a associação
# SCTP travou em 34% do teste de 100MB. Aqui a realimentação é o RTT medido pelas
# próprias sondas de latência sob carga, que já rodam em paralelo ao envio.
LOADED_LATENCY_TARGET_MS = 300   # alvo: folga sobre os 500ms, sem estrangular o link
PACING_BATCH = 50                # pacotes entre um ajuste e o seguinte (~70KB)
PACING_STEP = 0.002              # 2ms: piso do passo, e limiar pra zerar a pausa
# AIMD clássico não serve aqui: são só LATENCY_TEST_SIZE=20 sondas por fase, ou seja
# ~20 oportunidades de ajuste no teste inteiro. Com recuperação aditiva de 2ms o
# controlador sobe ao teto em 8 ajustes e não voltaria nunca — mediria 1Mbps num link
# de 8. Por isso os dois sentidos são multiplicativos: sobe x2, desce x0,5.
PACING_DECAY = 0.5               # fator de recuperação quando o RTT está confortável
# teto: 70KB por lote / 0,2s = ~2,8Mbps de piso. Abaixo disso não faz sentido — seria
# estrangular mais do que o link mais humilde que a ferramenta se propõe a medir.
PACING_MAX_PAUSE = 0.2
PACING_LOW_WATER = 0.7           # só volta a acelerar abaixo de 70% do alvo (histerese)

# --- recuperação de rodada ---
RETRY_INTERVAL_SECONDS = 10      # espera curta antes de reparear quando a rodada foi abortada
# rede de segurança pra rodada que trava SEM a conexão cair. Precisa ficar acima do
# pior caso legítimo: o test_complete do 100MB sozinho espera até 800s, e o upload de
# 100MB freado no teto do autoajuste (~2,8Mbps) leva ~5min — mais latência e perda de
# pacotes. (Os 800s do download que este comentário citava saíram em 2026-09-25: o
# receptor agora espera o sinal do remetente, sem cronômetro.)
ROUND_WATCHDOG_SECONDS = 45 * 60

# prazo da fase "conectando" (sinalização + ICE), cujo normal são ~6s. Antes ela caía
# no ROUND_WATCHDOG: em 2026-09-26 uma oferta ficou sem resposta e o peer ficaria
# 45min mudo. O watchdog continua valendo depois que a conexão sobe — é dimensionado
# pra rodada legítima longa, e não serve pra esta fase (ver utils.wait_round_outcome).
PAIRING_TIMEOUT_SECONDS = 60

# prazo pra o par RESPONDER a algo que ele já recebeu (o resultado calculado do meu
# upload; o END_TEST). A resposta leva segundos — a fila de 1MB drena em 8s no link
# de 1Mbps, mais RTT — e não a duração de uma transferência. Três esperas usavam
# `test_size / MIN_THROUGHPUT` (800s no 100MB) pra isso; em 2026-09-26 o cliente ficou
# 13min em send_ack_end_upload por um resultado que não chegou.
REPLY_TIMEOUT_SECONDS = 60

# IPFS topic name
IPFS_TOPIC = "tcc-polics"

# intervalo (segundos) entre o fim de um teste e o próximo pareamento (daemon)
TEST_INTERVAL_SECONDS = 30

# --- swarm connect: rendezvous via DHT ---
# O gossipsub só troca mensagem entre peers já conectados no swarm, e ele não
# descobre ninguém sozinho. Sem isso, dois peers publicam announce no vazio até
# que a DHT por acaso os aproxime. O daemon força esse encontro.
SWARM_INTERVAL_ALONE = 15        # nenhum peer no tópico: procura agressiva
SWARM_INTERVAL_TOGETHER = 60     # já tem com quem parear: só capta quem chegar depois
SWARM_PROVIDE_INTERVAL = 600     # reanuncia na DHT a cada 10 min
SWARM_FINDPROVS_TIMEOUT = 45     # a busca na DHT varre sem achar quando não há ninguém
SWARM_DIAL_TIMEOUT = 20          # peer inalcançável falha em ~13s; 20 dá folga
SWARM_NUM_PROVIDERS = 50         # generoso: registros de containers mortos ocupam vaga
SWARM_BASE_BACKOFF = 30          # espera após a 1ª falha, dobrando a cada nova
SWARM_MAX_BACKOFF = 600          # teto: o voluntário pode religar a máquina a qualquer hora