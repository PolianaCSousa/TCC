from aiortc import RTCPeerConnection, RTCSessionDescription
import asyncio
import socketio
import time
import json
import logging
import colorlog
from custom_types import Client, Server, Peer, Results
from utils import try_parse_json, event_timeout, events_timeout
from storage import save_to_file
from state import PeerState
from constants import (
    CONTROL, LATENCY, THROUGHPUT,
    END_LATENCY, END_THROUGHPUT, END_TEST,
    UPLOAD_RECEIVED, UPLOAD_ERROR, LAT_ACK_ERROR,
    LAT, LAT_ACK, ACK,
    LATENCY_TIMEOUT, SHORT_TIMEOUT,
    MIN_THROUGHPUT_BytePerSec,
    BYTES_PER_PACKAGE, BYTES_THROUGHPUT_10MB,
)

logger = logging.getLogger(__name__)

# creat a Socket.IO client and a peer for WebRTC connection
sio = socketio.AsyncClient()
peer = RTCPeerConnection()
state = PeerState()
server_peers: list[Peer] = []


# connect to server
@sio.event
async def connect():
    await sio.emit("connected")


# disconnect from server
@sio.event
async def disconnect():
    logger.info("Desconectado do servidor")


@sio.on("new_peer")
async def new_peer_on_server(data):
    server_peers.append(data)
    logger.debug("lista de pares do servidor: %s", server_peers)


@sio.on("snapshot")
async def server_snapshot(data):
    state.sid = data["sid"]
    state.results["sid"] = state.sid
    logger.debug("meu sid é %s", state.sid)
    server_peers.extend(data["snapshot"])

    # after receveing the snapshot from server the peer needs to remove itself from it's local list
    for peer in server_peers:
        if (peer["sid"] == data["sid"]):
            server_peers.remove(peer)
    logger.debug("snapshot recebido e tratado: %s", server_peers)
    await sio.emit("ready_to_start")


@sio.on("role_defined")
async def start_test(data):
    state.role = data["role"]
    state.results["role"] = state.role
    logger.info("Sou o %s", state.role)

    if state.role == "client":
        update_peers_list(data["peer"], 'server')
        logger.debug("lista local atualizada: %s", server_peers)
        target = data["peer"]["target"]
        await client_make_offer(target_name=target)
    else:
        update_peers_list(data["peer"], 'client')
        logger.debug("lista local atualizada: %s", server_peers)


# this method receives the answer from the server peer.
@sio.on("answer")
async def client_receives_answer(data):
    logger.info("answer do par servidor recebida no cliente")
    sdp = RTCSessionDescription(sdp=data["answer"]["sdp"], type=data["answer"]["type"])
    await peer.setRemoteDescription(sdp)  # this is the moment the connection is stablished


# client runs this method to make his offer to peer server
async def client_make_offer(target_name):
    @peer.on("icecandidate")
    def on_icecandidate(candidate):
        print("Client ICE candidate:", candidate)

    _create_client_data_channels()
    await _create_and_send_sdp_offer(target_name)
    _register_client_control_channel_handlers()
    _register_client_latency_channel_handlers()
    _register_client_throughput_channel_handlers()


# region Client methods
def _create_client_data_channels():
    # all channels must be created before the connection stablishment - these channels are created on client peer
    state.client["control_channel"] = peer.createDataChannel(CONTROL)
    state.client["throughput_channel"] = peer.createDataChannel(THROUGHPUT, maxPacketLifeTime=None, maxRetransmits=0,ordered=False)
    state.client["latency_channel"] = peer.createDataChannel(LATENCY)


async def _create_and_send_sdp_offer(target_name):
    offer = await peer.createOffer()
    await peer.setLocalDescription(offer)

    logger.info("oferta criada no par cliente")
    # print(f'debug - sdp do peer: {peer.localDescription.sdp}')
    await sio.emit("offer", {
        "to": target_name,
        "offer": {
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp
        }
    })


def _register_client_control_channel_handlers():
    @state.client["control_channel"].on("open")
    async def on_control_open():
        state.client["control_channel"].send("O teste de LATÊNCIA irá começar...")

    @state.client["control_channel"].on("message")
    async def on_control_message(message):
        msg = try_parse_json(message)
        logger.debug("upload (json): %s", msg)
        if message == END_LATENCY:
            logger.info("cliente recebeu END_LATENCY")
            state.events["latency_finished"].set()
        elif message == END_THROUGHPUT:
            state.events["throughput_finished"].set()
        elif message == UPLOAD_ERROR:
            state.events["upload_error"].set()
        elif message == END_TEST:
            logger.info("primeiro teste finalizado")
        elif msg is not None:
            state.results["upload"] = msg["value"]
            state.events["upload_received"].set()
        #else:
            #print(f"[CONTROLE]\t {message}")


def _register_client_latency_channel_handlers():
    @state.client["latency_channel"].on("open")
    async def on_latency_open():
        state.events["latency_finished"].clear()
        state.events["lat_ack_received"].clear()
        asyncio.create_task(client_send_lat_package(state.client["latency_channel"]))
        event_occured = await event_timeout(state.events["lat_ack_received"], LATENCY_TIMEOUT)
        if event_occured:
            logger.info("vou enviar o ack pro servidor")
            asyncio.create_task(client_send_ack(state.client["latency_channel"]))
            state.events["lat_ack_received"].clear()
        else:
            state.results["latency"] = None  
            state.client["control_channel"].send(LAT_ACK_ERROR)
    
    @state.client["latency_channel"].on("message")
    def on_latency_message(message):
        state.client["t1_latency"] = time.time_ns()
        state.events["lat_ack_received"].set()  # adicionei o set do evento depois de enviar o ack pra poder enviar o ack o mais rapido possivel de volta pro par servidor - CONFIRMAR COM EVERTHON
        calc_latency_a_b = (state.client["t1_latency"] - state.client["t0_latency"]) / (10 ** 6)

        if state.role == "client":
            state.results[LATENCY] = calc_latency_a_b

        logger.info("<<< recebi LAT-ACK")
        logger.info("LATÊNCIA DO CLIENTE a=>b %s ms", calc_latency_a_b)
      

def _register_client_throughput_channel_handlers():
    @state.client["throughput_channel"].on("open")
    async def on_throughput_open():
        event_occured = await event_timeout(state.events["latency_finished"],
                                            LATENCY_TIMEOUT)  # nesse caso, mesmo se o timeout estourar, eu posso prosseguir com o teste de vazão
        if event_occured:
            logger.info("latência finalizada. Vazão vai começar")
            state.client["control_channel"].send("O teste de VAZÃO irá começar...")
        else:
            state.client["control_channel"].send(
                "O teste de VAZÃO irá começar, apesar do cliente não ter recebido o pacote de fim de latência do par servidor.")
        asyncio.create_task(send_throughput_data(state.client["throughput_channel"], state.client["control_channel"], state.client,
                                                 BYTES_THROUGHPUT_10MB))  # TESTE COM 10MB por enquanto

        ## a task abaixo irá aguardar o evento upload_received ou upload_error
        bytes_to_be_sent = BYTES_THROUGHPUT_10MB
        asyncio.create_task(
            send_ack_end_upload(state.client["control_channel"], bytes_to_be_sent / MIN_THROUGHPUT_BytePerSec))

        ## a task abaixo irá aguardar o evento throughput_finished
        asyncio.create_task(
            calculate_throughput(state.role, state.client, state.events["throughput_finished"], bytes_to_be_sent / MIN_THROUGHPUT_BytePerSec))

    @state.client["throughput_channel"].on("message")
    def on_throughput_message(message):
        if state.client["qtd_packages"] == 0:
            state.client["t0_throughput"] = time.time()  # retorna o tempo em segundos
            # print(f'debug - tamanho do pacote recebido {len(message)}') #sys.getsizeof(package) retorna o tamanho do objeto Python na memória
        state.client["qtd_packages"] = state.client["qtd_packages"] + 1
# endregion 

# region Server Receives Offer
@sio.on("offer")
async def server_receives_offer(data):
    logger.debug("offer recebida no server_peer")
    await _create_and_send_sdp_answer(data)

    @peer.on("datachannel")
    def on_datachannel(received_channel):
        if received_channel.label == CONTROL:
            state.server["channels"][CONTROL] = received_channel
            _register_server_control_channel_handler()
        elif received_channel.label == LATENCY:
            state.server["channels"][LATENCY] = received_channel
            _register_server_latency_channel_handler()
        elif received_channel.label == THROUGHPUT:
            state.server["channels"][THROUGHPUT] = received_channel
            # TESTE SÓ PRA VER SE CORRIGE MESMO
            state.server["qtd_total_bytes"] = BYTES_THROUGHPUT_10MB
            # a task abaixo espera o envio dos dados terminar - throughput_finished
            asyncio.create_task(calculate_throughput(state.role, state.server, state.events["throughput_finished"]))
            _register_server_throughput_channel_handler()
# endregion

# region Server methods
async def _create_and_send_sdp_answer(data):
    sdp = RTCSessionDescription(sdp=data["offer"]["sdp"], type=data["offer"]["type"])
    await peer.setRemoteDescription(sdp)

    answer = await peer.createAnswer()
    logger.debug("answer criada no server_peer")
    await peer.setLocalDescription(answer)

    await sio.emit("answer", {
        "to": data["from"],  # data["from"] é o sid do client_peer OK
        "answer": {
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp
        }
    })


def _register_server_control_channel_handler():
    @state.server["channels"][CONTROL].on("message")
    async def on_control_message(message):
        print(f'[CONTROLE] {message}')
        msg = try_parse_json(message)
        if message == END_THROUGHPUT:
            state.events["throughput_finished"].set()
        if message == UPLOAD_RECEIVED:
            state.events["start_server_throughput"].set()
        if message == UPLOAD_ERROR:
            state.events["upload_error"].set()
        if message == LAT_ACK_ERROR:
            state.events["lat_ack_error"].set()
        if msg is not None:
            state.results["upload"] = msg["value"]  # It's here when the tests finish for server
            save_to_file(state.results)
            state.events["upload_received"].set()  # aqui preciso tratar
            logger.info("Resultados do servidor: %s", state.results)
        logger.debug("upload (json): %s", msg)


def _register_server_latency_channel_handler():
    @state.server["channels"][LATENCY].on("message")
    def on_latency_message(message):
        if message == LAT:
            state.events["ack_received"].clear()
            state.events["lat_ack_error"].clear()
            asyncio.create_task(server_send_lat_ack(state.server["channels"][LATENCY]))
            asyncio.create_task(handle_server_latency_timeout(state.server["channels"][CONTROL], LATENCY_TIMEOUT))
            logger.info("<<< recebi LAT")
        else:  # se ACK for recebido com sucesso
            state.server["t1_latency"] = time.time_ns()
            state.events["ack_received"].set()
            calc_latency_b_a = (state.server["t1_latency"] - state.server["t0_latency"]) / (10 ** 6)

            if state.role == 'server':
                state.results[LATENCY] = calc_latency_b_a

            logger.info("<<< recebi ACK")
            logger.info("LATÊNCIA DO SERVIDOR b=>a %s ms", calc_latency_b_a)

            # SERVER["channels"][CONTROL].send(f'LATÊNCIA b=>a {calc_latency_b_a} ms')
            state.server["channels"][CONTROL].send(END_LATENCY)


def _register_server_throughput_channel_handler():
    @state.server["channels"][THROUGHPUT].on("message")
    def on_throughput_message(message):
        if state.server["qtd_packages"] == 0:
            state.server["t0_throughput"] = time.time()  # retorna o tempo em segundos
        state.server["qtd_packages"] = state.server["qtd_packages"] + 1
# endregion

async def send_throughput_data(throughput_channel, control_channel, PEER, test_size):
    try:
        package = bytes(BYTES_PER_PACKAGE)
        # as duas linhas a seguir podem virar so uma
        # tam_total_dados = BYTES_THROUGHPUT_10MB  # enviarei no total 10MB = 10.000.000 Bytes
        PEER["qtd_total_bytes"] = test_size
        qtd_pacotes = test_size // len(package)
        tam_pacote = len(package)
        logger.debug("o envio dos pacotes vai começar agora. Vou enviar %s pacotes de tamanho %s", qtd_pacotes, tam_pacote)
        logger.debug("ICE: %s", peer.iceConnectionState)
        logger.debug("DTLS: %s", peer.connectionState)
        for i in range(0, qtd_pacotes):
            throughput_channel.send(package)
        # throughput_channel.send(END_THROUGHPUT)
        control_channel.send(END_THROUGHPUT)
    except Exception as e:
        print(f'Erro no envio dos dados da vazão: {e}')


async def calculate_throughput(role, PEER, throughput_finished, timeout=5):
    total_bytes_esperada = PEER[
        "qtd_total_bytes"]  ## ex.: teria o BYTES_THROUGHPUT_10MB como o valor dessa chave tam_bytes_test
    timeout = total_bytes_esperada / MIN_THROUGHPUT_BytePerSec
    response = await event_timeout(throughput_finished, timeout)
    if response:
        PEER["t1_throughput"] = time.time()
        tempo = PEER["t1_throughput"] - PEER["t0_throughput"]
        vazao_em_bytes = ((PEER["qtd_packages"] - 1) * BYTES_PER_PACKAGE) / tempo  # 1400 é o tamanho do pacote
        vazao_em_MB = round(vazao_em_bytes / 10 ** 6, 2)
        vazao_em_Mbps = vazao_em_MB * 8
        if role == "server":
            state.results["download"] = vazao_em_Mbps
            # SERVER["channels"][CONTROL].send(f'RESULTADO DO TESTE DE UPLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
            state.server["channels"][CONTROL].send(json.dumps({
                "msg": "upload",
                "value": vazao_em_Mbps
            }))
            logger.info("RESULTADO DO TESTE DE DOWNLOAD: \n A vazão calculada é de %s Mb/s", vazao_em_Mbps)
            response = await event_timeout(state.events["start_server_throughput"], SHORT_TIMEOUT)
            if response:
                state.server["channels"][CONTROL].send(
                    "Não recebi o ACK do resultado do upload do cliente. Vou iniciar o teste mesmo assim.")
            else:
                state.server["channels"][CONTROL].send("Recebi ACK do upload do cliente. Vou iniciar o teste agora.")
            asyncio.create_task(send_throughput_data(state.server["channels"][THROUGHPUT], state.server["channels"][CONTROL], state.server,
                                                     BYTES_THROUGHPUT_10MB))  # TESTE COM 10MB por enquanto
            ## a task abaixo irá aguardar o evento upload_received ou upload_error
            bytes_to_be_sent = BYTES_THROUGHPUT_10MB
            asyncio.create_task(send_end_test(state.server["channels"][CONTROL], bytes_to_be_sent / MIN_THROUGHPUT_BytePerSec))
        else:
            logger.debug("sou cliente e ja tenho o download: %s Mbps", vazao_em_Mbps)
            state.results["download"] = vazao_em_Mbps  # It's here when the tests finish for client
            logger.info("Resultados do cliente: %s", state.results)
            save_to_file(state.results)
            state.client["control_channel"].send(
                f'RESULTADO DO TESTE DE UPLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
            state.client["control_channel"].send(json.dumps({
                "msg": "upload",
                "value": vazao_em_Mbps
            }))
            logger.info("RESULTADO DO TESTE DE DOWNLOAD: \n A vazão calculada é de %s Mb/s", vazao_em_Mbps)
    else:
        # meu download é none e o do outro par é none o upload
        state.results["download"] = None
        if role == "server":
            state.server["channels"][CONTROL].send(UPLOAD_ERROR)
        else:
            state.client["control_channel"].send(UPLOAD_ERROR)


# region Calculate and send latency package
async def server_send_lat_ack(latency_channel):
    state.server["t0_latency"] = time.time_ns()
    latency_channel.send(LAT_ACK)
    logger.info(">>> enviei LAT_ACK")
# endregion


async def client_send_lat_package(latency_channel):
    state.client["t0_latency"] = time.time_ns()
    latency_channel.send(LAT)
    logger.info(">>> enviei LAT")


async def client_send_ack(latency_channel):
    latency_channel.send(ACK)
    logger.info(">>> enviei ACK")


def update_peers_list(this, role):
    for peer in server_peers:
        if peer["sid"] == this["target"]:
            peer["role"] = role
            peer["status"] = "OCCUPIED"
            peer["target"] = state.sid


async def send_ack_end_upload(control_channel, timeout):
    response = await events_timeout({"upload_received": state.events["upload_received"],
                                     "upload_error": state.events["upload_error"]
                                     }, timeout)
    if response == "upload_received":
        control_channel.send(UPLOAD_RECEIVED)
    else:
        if state.results["upload"] is not None:
            state.results["upload"] = None
        if state.role == "client":
            control_channel.send(UPLOAD_RECEIVED)  # vou enviar mesmo que tenha dado errado pra que o teste continue
        else:
            control_channel.send(UPLOAD_ERROR)


async def send_end_test(control_channel, timeout):
    response = await events_timeout({"upload_received": state.events["upload_received"],
                                     "upload_error": state.events["upload_error"]
                                     }, timeout)
    if response == "upload_error" and state.results["upload"] is not None:
        state.results["upload"] = None
    control_channel.send(END_TEST)  # somente aqui eu envio o fim do teste, quando da certo ou quando da errado


async def handle_server_latency_timeout(control_channel, timeout):
    # event_ocurred = await event_timeout
    response = await events_timeout({"ack_received": state.events["ack_received"],
                                     "lat_ack_error": state.events["lat_ack_error"]
                                     }, timeout)
    if response != "ack_received":
        if not state.results[LATENCY]:
            # state.results[LATENCY] = None
            control_channel.send(END_LATENCY)


# Função principal para iniciar o cliente e conectar
async def main():
    # ENTENDER MELHOR ESSA CONFIG DO LOG
    # log configuration
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "white",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        }
    ))
    # raiz silenciosa (só WARNING+ das libs externas como aiortc/aioice)
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    # libera só o meu módulo
    logger.setLevel(logging.INFO)

    # Conectando ao servidor
    await sio.connect('http://localhost:5000')

    # print("Conectando... Aguarde o canal ser estabelecido.")

    try:
        await sio.wait()
    except KeyboardInterrupt:
        print("\nSaindo...")
        # control_task.cancel()
        await sio.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
