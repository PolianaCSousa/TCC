# --- peer status ---
FREE = "FREE"
OCCUPIED = "OCCUPIED"

# --- label for channels ---
CONTROL = "control"
LATENCY = "latency"
THROUGHPUT = "throughput"
PACKAGE_LOSS = "package_loss"

# --- flags for acks and end of tests ---
END_LATENCY = "Fim latência"
START_THROUGHPUT = "Os testes de vazão irão começar"
END_THROUGHPUT = "fim"
UPLOAD_RECEIVED = "upload_received"
LAT_ACK_ERROR = "Erro na latência. O pacote LAT_ACK não foi entregue."
UPLOAD_ERROR = "Upload não foi recebido"
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

# --- timeouts ---
_ACCEPTABLE_LATENCY_MS = 80
LATENCY_TIMEOUT = 10 * (_ACCEPTABLE_LATENCY_MS / 1000)  # estou considerando que a latencia aceitável é de 80ms. Meu timeout vai esperar até 10 vezes isso.
LOADED_LATENCY_TIMEOUT = 2 
SHORT_TIMEOUT = 12 * (_ACCEPTABLE_LATENCY_MS / 1000)  # criei esse timeout pra esperar os acks - MOSTRAR EVERTHON

# LATENCY_PROBE_INTERVAL = 0.001 # 10 ms
LATENCY_PROBE_INTERVAL = 0.002 # 20 ms
