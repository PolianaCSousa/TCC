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

# --- packet loss test ---
# sent in spaced batches: a single burst would cause the loss itself
PACKAGE_LOSS_TOTAL = 1000
PACKAGE_LOSS_BATCH = 50
PACKAGE_LOSS_PAUSE = 0.1
LATENCY_TEST_SIZE = 20

# --- throughput test size ---
BYTES_THROUGHPUT_100KB = 100 * 10 ** 3
BYTES_THROUGHPUT_1MB = 1 * 10 ** 6
BYTES_THROUGHPUT_10MB = 10 * 10 ** 6
BYTES_THROUGHPUT_50MB = 50 * 10 ** 6
THROUGHPUT_LABELS = {
    BYTES_THROUGHPUT_100KB: "100KB",
    BYTES_THROUGHPUT_1MB: "1MB",
    BYTES_THROUGHPUT_10MB: "10MB",
    BYTES_THROUGHPUT_50MB: "50MB",
}

# --- round status ---
COMPLETE = "complete"
ABORTED = "aborted"

# more data is queued only while the buffer is below this limit
BUFFER_AMOUNT_LIMIT = {
    BYTES_THROUGHPUT_100KB: 20 * 10 ** 3,
    BYTES_THROUGHPUT_1MB: 100 * 10 ** 3,
    BYTES_THROUGHPUT_10MB: 1 * 10 ** 6,
    BYTES_THROUGHPUT_50MB: 1 * 10 ** 6,
}

# lowest throughput expected among users (1 Mbps)
__MIN_THROUGHPUT_MbitPerSec = 1
MIN_THROUGHPUT_BytePerSec = (__MIN_THROUGHPUT_MbitPerSec / 8) * 10 ** 6

# --- package size ---
# same as aiortc's USERDATA_MAX_LENGTH: each packet fits in one SCTP chunk
BYTES_PER_PACKAGE = 1200

# --- heartbeat ---
HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_PACKAGE_SIZE = 1200

# --- timeouts ---
_ACCEPTABLE_LATENCY_MS = 80
LATENCY_TIMEOUT = 10 * (_ACCEPTABLE_LATENCY_MS / 1000)  # 10x the acceptable latency
LOADED_LATENCY_TIMEOUT = 2 
SHORT_TIMEOUT = 12 * (_ACCEPTABLE_LATENCY_MS / 1000)

LATENCY_PROBE_INTERVAL = 0.002

# --- throughput test flow control ---
BUFFER_DRAIN_TIMEOUT = 5         # seconds to wait, each time, for bufferedamountlow
MAX_BUFFER_STALLS = 6            # consecutive waits without draining before aborting the send

# rate detector: below the floor within the window, SCTP is considered stalled
STALL_WINDOW_SECONDS = 30
STALL_RATE_FRACTION = 0.1
STALL_FLOOR_BytePerSec = MIN_THROUGHPUT_BytePerSec * STALL_RATE_FRACTION   # 12,500 B/s

# --- delay-based send pacing (simplified Vegas/BBR-style control) ---
LOADED_LATENCY_TARGET_MS = 300   # target RTT under load
PACING_BATCH = 50                # packets between adjustments
PACING_STEP = 0.002              # minimum pause step (s)
PACING_DECAY = 0.5               # pause reduction factor when RTT is below the target
PACING_MAX_PAUSE = 0.2           # maximum pause per batch (s)
PACING_LOW_WATER = 0.7           # speeds up again only below 70% of the target (hysteresis)

# --- round recovery ---
RETRY_INTERVAL_SECONDS = 50      # wait before re-pairing after an aborted round
ROUND_WATCHDOG_SECONDS = 45 * 60 # limit for a round that hangs without the connection dropping

# deadline for the connection phase (signaling + ICE)
PAIRING_TIMEOUT_SECONDS = 60

# deadline for the peer to reply to a control message
REPLY_TIMEOUT_SECONDS = 60

IPFS_TOPIC = "tcc-polics"

# cycle: round -> SPEEDTEST_DELAY -> Speedtest -> TEST_INTERVAL -> repeat
SPEEDTEST_DELAY_SECONDS = 10
SPEEDTEST_TIMEOUT_SECONDS = 120

# interval (seconds) between the end of the Speedtest and the next pairing
TEST_INTERVAL_SECONDS = 150

# --- swarm connect: rendezvous via DHT ---
SWARM_INTERVAL_ALONE = 15        # no peers in the topic
SWARM_INTERVAL_TOGETHER = 60     # peers already in the topic
SWARM_PROVIDE_INTERVAL = 600     # re-announces on the DHT every 10 min
SWARM_FINDPROVS_TIMEOUT = 45
SWARM_DIAL_TIMEOUT = 20
SWARM_NUM_PROVIDERS = 50
SWARM_BASE_BACKOFF = 30          # doubles on each new failure
SWARM_MAX_BACKOFF = 600