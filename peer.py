from aiortc import RTCPeerConnection, RTCSessionDescription
import asyncio
import socketio
import time
import logging
import colorlog
from custom_types import Client, Server, Peer, Results
from utils import try_parse_json, event_timeout, update_peers_list
from storage import save_to_file
from state import state
from constants import (
    CONTROL, LATENCY, THROUGHPUT,
    END_LATENCY, END_THROUGHPUT, END_TEST,
    UPLOAD_RECEIVED, UPLOAD_ERROR, LAT_ACK_ERROR,
    LAT, LAT_ACK, ACK,
    LATENCY_TIMEOUT, SHORT_TIMEOUT,
    MIN_THROUGHPUT_BytePerSec,
    BYTES_PER_PACKAGE, BYTES_THROUGHPUT_10MB,
)
from experiments.latency import(
    server_send_lat_ack,
    client_send_lat_package,
    client_send_ack,
    handle_server_latency_timeout)
from experiments.throughput import(
    send_throughput_data,
    calculate_throughput,
    send_ack_end_upload
)

logger = logging.getLogger(__name__)

# creat a Socket.IO client and a peer for WebRTC connection
sio = socketio.AsyncClient()
peer = RTCPeerConnection()
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
        update_peers_list(data["peer"], 'server', server_peers)
        logger.debug("lista local atualizada: %s", server_peers)
        target = data["peer"]["target"]
        await client_make_offer(target_name=target)
    else:
        update_peers_list(data["peer"], 'client', server_peers)
        logger.debug("lista local atualizada: %s", server_peers)


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


# this method receives the answer from the server peer.
@sio.on("answer")
async def client_receives_answer(data):
    logger.info("answer do par servidor recebida no cliente")
    sdp = RTCSessionDescription(sdp=data["answer"]["sdp"], type=data["answer"]["type"])
    await peer.setRemoteDescription(sdp)  # this is the moment the connection is stablished


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
    logging.getLogger("experiments").setLevel(logging.INFO)

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
