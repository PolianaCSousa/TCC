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
    LAT, LATENCY_TIMEOUT, 
    MIN_THROUGHPUT_BytePerSec, BYTES_THROUGHPUT_10MB, START_THROUGHPUT,
    BYTES_THROUGHPUT_100KB, BYTES_THROUGHPUT_100MB, BYTES_THROUGHPUT_1MB,
    END_ITERATION, END_LAT_PACKAGES
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
        #logger.info("received (json): %s", msg)
        if message == END_LATENCY:
            logger.info("cliente recebeu END_LATENCY")
            state.events["latency_finished"].set()
        elif message == END_ITERATION:
            state.events["end_iteration"].set()
        elif message == END_THROUGHPUT:
            state.events["throughput_finished"].set()
        elif message == UPLOAD_ERROR:
            state.events["upload_error"].set()
        elif message == END_TEST:
            logger.info("------ TESTE FINALIZADO ------")
            state.events["test_complete"].set()
        elif msg is not None:
            state.results["upload"] = msg["value"]
            state.results["test_size"] = msg["test_size"]
            state.events["upload_received"].set()
        #else:
            #print(f"[CONTROLE]\t {message}")


def _register_client_latency_channel_handlers():
    @state.client["latency_channel"].on("open")
    async def on_latency_open():
        await client_latency(20)
        await calculate_client_latency(20) #assim que o cliente termina de enviar ele ja pode calcular sem problema, o que nao pode acontecer é ele começar o teste de vazão antes do servidor terminar de calcular a latência dele
        #quando o servidor terminar de salvar o resultado dele, ele envia o END_LATENCY e no canal de controle eu seto latency_finished

    @state.client["latency_channel"].on("message")
    def on_latency_message(message):
        state.client["t1_latency"].append(time.time_ns())
        state.events["lat_ack_received"].set()  # adicionei o set do evento depois de enviar o ack pra poder enviar o ack o mais rapido possivel de volta pro par servidor
      

def _register_client_throughput_channel_handlers():
    @state.client["throughput_channel"].on("open")
    async def on_throughput_open():
        event_occured = await event_timeout(state.events["latency_finished"],
                                            LATENCY_TIMEOUT)  # nesse caso, mesmo se o timeout estourar, eu posso prosseguir com o teste de vazão
        if event_occured:
            #logger.info("latência finalizada. Vazão vai começar")
            state.client["control_channel"].send(START_THROUGHPUT)
        else:
            #O teste de VAZÃO irá começar, apesar do cliente não ter recebido o pacote de fim de latência do par servidor
            state.client["control_channel"].send(START_THROUGHPUT)
            
        await calculate_client_throughput(BYTES_THROUGHPUT_100KB)
        await calculate_client_throughput(BYTES_THROUGHPUT_1MB)
        await calculate_client_throughput(BYTES_THROUGHPUT_10MB)
        await calculate_client_throughput(BYTES_THROUGHPUT_100MB)
        logger.info("---------------- FIM DO EXPERIMENTO ----------------")
        

    @state.client["throughput_channel"].on("message")
    def on_throughput_message(message):
        if state.client["qtd_packages"] == 0:
            state.client["t0_throughput"] = time.time()  # retorna o tempo em segundos
        state.client["qtd_packages"] = state.client["qtd_packages"] + 1


async def calculate_client_throughput(test_size):
    state.reset_for_test()
    await calculate_client_upload(test_size)
    await calculate_client_download(test_size)
    await event_timeout(state.events["test_complete"], test_size / MIN_THROUGHPUT_BytePerSec) #wait for the test finish completely


async def calculate_client_upload(test_size):
    await send_throughput_data(state.client["throughput_channel"], state.client["control_channel"], state.client,test_size)
    ## a task abaixo irá aguardar o evento upload_received ou upload_error
    await send_ack_end_upload(state.client["control_channel"], test_size / MIN_THROUGHPUT_BytePerSec)


async def calculate_client_download(test_size):
    ## a task abaixo irá aguardar o evento throughput_finished
    await calculate_throughput(state.role, state.client, state.events["throughput_finished"], test_size / MIN_THROUGHPUT_BytePerSec)
        

async def client_latency(qtd_tests):
    for _ in range(qtd_tests):
        state.events["latency_finished"].clear()
        state.events["lat_ack_received"].clear()
        state.events["end_iteration"].clear()
        await client_send_lat_package(state.client["latency_channel"])

        event_occured = await event_timeout(state.events["lat_ack_received"], LATENCY_TIMEOUT)
        if event_occured:
            #logger.info("vou enviar o ack pro servidor")
            await client_send_ack(state.client["latency_channel"])
        else:
            state.client["control_channel"].send(LAT_ACK_ERROR)
        #ESPERAR O END_ITERATION
        await event_timeout(state.events["end_iteration"], LATENCY_TIMEOUT)
    state.client["control_channel"].send(END_LAT_PACKAGES)


async def calculate_client_latency(qtd_tests):
    lat_sum = 0
    for i in range(qtd_tests):
        lat_sum = lat_sum + (state.client["t1_latency"][i] - state.client["t0_latency"][i])
    latency = (lat_sum / qtd_tests) / 10**6 #converte de ns para ms
    state.results[LATENCY] = round(latency,2)

#def calculate_client_latency():

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
        if message == START_THROUGHPUT: #pode ser que essa mensagem nao chegue, e aí seria um problema, mas o tratamento seria feito no canal webRTC
            asyncio.create_task(_calculate_server_download());
        elif message == END_LAT_PACKAGES:
            calculate_server_latency(20)
        elif message == END_THROUGHPUT:
            state.events["throughput_finished"].set()
        elif message == UPLOAD_RECEIVED:
            state.events["start_server_upload"].set()
        elif message == UPLOAD_ERROR:
            state.events["upload_error"].set()
        elif message == LAT_ACK_ERROR:
            state.events["lat_ack_error"].set()
        elif msg is not None:
            logger.info("Upload do servidor: %s", msg["value"])
            state.results["upload"] = msg["value"]  # It's here when the tests finish for server
            save_to_file(state.results)
            state.events["upload_received"].set()
            logger.info("Resultados do servidor: %s", state.results)


def _register_server_latency_channel_handler():
    @state.server["channels"][LATENCY].on("message")
    async def on_latency_message(message):
        await server_latency(message) #o metodo é chamado sempre que uma mensagem chega nessa canal, logo eu nao posso chamar o calculate_server latency aqui


def _register_server_throughput_channel_handler():
    @state.server["channels"][THROUGHPUT].on("message")
    def on_throughput_message(message):
        if state.server["qtd_packages"] == 0:
            state.server["t0_throughput"] = time.time()  # retorna o tempo em segundos
        state.server["qtd_packages"] = state.server["qtd_packages"] + 1


async def _calculate_server_download():
    await _calculate_download(BYTES_THROUGHPUT_100KB)
    await _calculate_download(BYTES_THROUGHPUT_1MB)
    await _calculate_download(BYTES_THROUGHPUT_10MB)
    await _calculate_download(BYTES_THROUGHPUT_100MB)
    logger.info("---------------- FIM DO EXPERIMENTO ----------------")


async def _calculate_download(test_size):
    state.reset_for_test()
    state.server["qtd_total_bytes"] = test_size
    # a task abaixo espera o envio dos dados (do cliente) terminar - throughput_finished
    await calculate_throughput(state.role, state.server, state.events["throughput_finished"])


async def server_latency(message):
    if message == LAT:
        #logger.info("<<< recebi LAT")
        state.events["ack_received"].clear()
        state.events["lat_ack_error"].clear()
        await server_send_lat_ack(state.server["channels"][LATENCY])
        await handle_server_latency_timeout(state.server["channels"][CONTROL], LATENCY_TIMEOUT)
    else:  # se ACK for recebido com sucesso
        state.server["t1_latency"].append(time.time_ns())
        state.events["ack_received"].set()
        #logger.info("<<< recebi ACK")
        state.server["channels"][CONTROL].send(END_ITERATION)


def calculate_server_latency(qtd_tests):
    lat_sum = 0
    for i in range(qtd_tests):
        lat_sum = lat_sum + (state.server["t1_latency"][i] - state.server["t0_latency"][i])
    latency = (lat_sum / qtd_tests) / 10**6
    state.results[LATENCY] = round(latency, 2)
    state.server["channels"][CONTROL].send(END_LATENCY)
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
