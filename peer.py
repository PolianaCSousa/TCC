from aiortc import RTCPeerConnection, RTCSessionDescription
import asyncio
import socketio
import time
import logging
import colorlog
import json
from custom_types import Client, Server, Peer, Results
from utils import try_parse_json, event_timeout, update_peers_list
from storage import save_to_file
from state import state
from constants import (
    CONTROL, LATENCY, THROUGHPUT, PACKAGE_LOSS,
    END_LATENCY, END_THROUGHPUT, END_TEST, START_LOADED_PACKAGES, END_LOADED_PACKAGES, LOADED_LATENCY, LATENCY_PROBE_INTERVAL, LATENCY_TEST_SIZE,
    UPLOAD_RECEIVED, UPLOAD_ERROR, LAT_ACK_ERROR, PACKAGE_LOSS_TIMEOUT,
    LAT, LATENCY_TIMEOUT, 
    MIN_THROUGHPUT_BytePerSec, BYTES_THROUGHPUT_10MB, START_THROUGHPUT,
    BYTES_THROUGHPUT_100KB, BYTES_THROUGHPUT_100MB, BYTES_THROUGHPUT_1MB,
    END_ITERATION, END_LAT_PACKAGES, END_PACKAGE_LOSS, ACK_PACKAGE_LOSS,
    THROUGHPUT_LABELS, BUFFER_AMOUNT_LIMIT
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
    server_peers.append(data)#ONDE PAREI: preciso adicionar timeout pra perda de pacote e testar 
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
    _register_client_package_loss_channel_handlers()


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
    state.client["package_loss_channel"] = peer.createDataChannel(PACKAGE_LOSS, maxPacketLifeTime=None, maxRetransmits=0, ordered=False)


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
        elif message == END_PACKAGE_LOSS:
            client_calculates_server_package_loss()
        elif msg is not None and msg["msg"] == 'upload':
            label = THROUGHPUT_LABELS[msg["test_size"]]
            state.results[f"{label}_upload"] = msg["value"]
            state.events["upload_received"].set()
        elif msg is not None and msg["msg"] == 'package_loss':
            state.results["package_loss"] = msg["value"]
            state.events["package_loss_received"].set()
            state.client["control_channel"].send(ACK_PACKAGE_LOSS)
            logger.info("Perda de pacotes do cliente: %s", state.results["package_loss"])

        #else:
            #print(f"[CONTROLE]\t {message}")


def _register_client_latency_channel_handlers():
    @state.client["latency_channel"].on("open")
    async def on_latency_open():
        await client_latency(LATENCY_TEST_SIZE, LATENCY, LATENCY_PROBE_INTERVAL)
        state.client["control_channel"].send(END_LAT_PACKAGES)
        await calculate_client_latency(LATENCY_TEST_SIZE) #assim que o cliente termina de enviar ele ja pode calcular sem problema, o que nao pode acontecer é ele começar o teste de vazão antes do servidor terminar de calcular a latência dele
        #quando o servidor terminar de salvar o resultado dele, ele envia o END_LATENCY e no canal de controle eu seto latency_finished

    @state.client["latency_channel"].on("message")
    def on_latency_message(message):
        t0 = state.client[state.t0_latency_key()]
        t1 = state.client[state.t1_latency_key()]
        if len(t1) < len(t0):
            t1.append(time.time_ns())
            state.events["lat_ack_received"].set()
      

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
        state.events["end_throughput_experiments"].set()
        

    @state.client["throughput_channel"].on("message")
    def on_throughput_message(message):
        if state.client["qtd_packages"] == 0:
            state.client["t0_throughput"] = time.time()  # retorna o tempo em segundos
        state.client["qtd_packages"] = state.client["qtd_packages"] + 1
    

    @state.client["throughput_channel"].on("bufferedamountlow")
    def on_throughput_buffer_amount_low():
        state.events["throughput_buffer_drained"].set()



def _register_client_package_loss_channel_handlers():
    @state.client["package_loss_channel"].on("open")
    async def on_package_loss_open():
        await state.events["end_throughput_experiments"].wait() #espera o fim dos testes de latencia e vazão independentemente do tempo que eles irão gastar
        await client_package_loss()
    
    @state.client["package_loss_channel"].on("message")
    def on_package_loss_message(message):
        state.client["received_packages"] = state.client["received_packages"] + 1
        

async def calculate_client_throughput(test_size):
    state.reset_for_test()
    state.client["throughput_channel"].bufferedAmountLowThreshold = BUFFER_AMOUNT_LIMIT[test_size]
    await calculate_client_upload(test_size)
    await calculate_client_download(test_size)
    await event_timeout(state.events["test_complete"], test_size / MIN_THROUGHPUT_BytePerSec) #wait for the test finish completely


async def calculate_client_upload(test_size):
    loaded_latency_task = asyncio.create_task(client_latency(LATENCY_TEST_SIZE, LOADED_LATENCY, LATENCY_PROBE_INTERVAL, test_size))
    await send_throughput_data(state.client["throughput_channel"], state.client["control_channel"], state.client,test_size)
    ## a task abaixo irá aguardar o evento upload_received ou upload_error
    await send_ack_end_upload(state.client["control_channel"], test_size / MIN_THROUGHPUT_BytePerSec, test_size)
    await loaded_latency_task


async def calculate_client_download(test_size):
    ## a task abaixo irá aguardar o evento throughput_finished
    await calculate_throughput(state.role, state.client, state.events["throughput_finished"], test_size / MIN_THROUGHPUT_BytePerSec)
        

async def client_latency(qtd_tests, type=LATENCY, sleep_loaded_interval=0, test_size=None):
    state.latency_type = "loaded" if (type == LOADED_LATENCY) else "unloaded"
    if type == LOADED_LATENCY:
        state.client["control_channel"].send(START_LOADED_PACKAGES)
    for _ in range(qtd_tests):
        state.events["latency_finished"].clear()
        state.events["loaded_latency_finished"].clear()
        state.events["lat_ack_received"].clear()
        state.events["end_iteration"].clear()
        await client_send_lat_package(state.client["latency_channel"])

        event_occured = await event_timeout(state.events["lat_ack_received"], LATENCY_TIMEOUT)
        if event_occured:
            #logger.info("vou enviar o ack pro servidor")
            await client_send_ack(state.client["latency_channel"])
        else:
            state.client[state.t1_latency_key()].append(None) #se o LAT nao chegar no servidor, eu nem vou receber o LAT_ACK, logo meu t1_latency fica sendo None
            state.client["control_channel"].send(LAT_ACK_ERROR)
        #ESPERAR O END_ITERATION
        await event_timeout(state.events["end_iteration"], LATENCY_TIMEOUT)

        if sleep_loaded_interval:
            await asyncio.sleep(sleep_loaded_interval)
    
    if type == LOADED_LATENCY:
        state.client["control_channel"].send(END_LOADED_PACKAGES)
        await calculate_client_latency(LATENCY_TEST_SIZE, LOADED_LATENCY, test_size)
        

async def calculate_client_latency(qtd_tests, result_key=LATENCY, test_size=None):
    lat_sum = 0
    qtd_received = min(len(state.client[state.t0_latency_key()]), len(state.client[state.t1_latency_key()]))
    qtd_received = min(qtd_tests, qtd_received)
    valid_packages = 0
    for i in range(qtd_received):
        if state.client[state.t1_latency_key()][i] is not None:
            lat_sum = lat_sum + (state.client[state.t1_latency_key()][i] - state.client[state.t0_latency_key()][i])
            valid_packages = valid_packages + 1
        else:
            continue 
        logger.info(f'===> qtd_tests={qtd_tests} \t i={i} of range={qtd_received}')

    col = LATENCY if result_key == LATENCY else f"{THROUGHPUT_LABELS[test_size]}_loaded_latency"
    if valid_packages > 0:
        latency = (lat_sum / valid_packages) / 10**6 #converte de ns para ms
        state.results[col] = round(latency,2)
    else:
        state.results[col] = None

    if result_key != LATENCY:
        state.reset_loaded_latency(state.client)


async def client_package_loss():
    state.events["package_loss_received"].clear()
    state.events["end_throughput_experiments"].clear()
    package = bytes(1)
    for _ in range(1000):
        state.client["package_loss_channel"].send(package)
    await asyncio.sleep(2)
    state.client["control_channel"].send(END_PACKAGE_LOSS)
    event_ocurred = await event_timeout(state.events["package_loss_received"], PACKAGE_LOSS_TIMEOUT)
    if not event_ocurred:
        state.results["package_loss"] = None
    save_to_file(state.results)
    state.reset_results()

def client_calculates_server_package_loss():
    received_packages = state.client["received_packages"]
    lost_packages = 1000 - received_packages
    package_loss = (lost_packages/1000) * 100
    state.client["control_channel"].send(json.dumps({
                "msg": "package_loss",
                "value": package_loss,
            }))
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
        elif received_channel.label == PACKAGE_LOSS:
            state.server["channels"][PACKAGE_LOSS] = received_channel
            _register_server_package_loss_channel_handler()
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
            calculate_server_latency(LATENCY_TEST_SIZE)
        elif message == START_LOADED_PACKAGES:
            state.latency_type = "loaded"
        elif message == END_LOADED_PACKAGES:
            calculate_server_latency(LATENCY_TEST_SIZE, LOADED_LATENCY, state.server["qtd_total_bytes"])
        elif message == END_THROUGHPUT:
            state.events["throughput_finished"].set()
        elif message == UPLOAD_RECEIVED:
            state.events["start_server_upload"].set()
        elif message == UPLOAD_ERROR:
            state.events["upload_error"].set()
        elif message == LAT_ACK_ERROR:
            state.events["lat_ack_error"].set()
        elif message == END_PACKAGE_LOSS:
            await server_calculates_client_package_loss()
        elif message == ACK_PACKAGE_LOSS:
            state.events["ack_package_loss_received"].set()
        elif msg is not None and msg["msg"] == 'upload':
            logger.info("Upload do servidor: %s", msg["value"])
            label = THROUGHPUT_LABELS[msg["test_size"]]
            state.results[f"{label}_upload"] = msg["value"]  # It's here when the tests finish for server
            state.events["upload_received"].set()
            logger.info("Resultados do servidor: %s", state.results)
        elif msg is not None and msg["msg"] == 'package_loss':
            state.results["package_loss"] = msg["value"]
            state.events["package_loss_received"].set()
            logger.info("Perda de pacotes do servidor: %s", state.results["package_loss"])
            logger.info("---------------- FIM DO EXPERIMENTO ----------------")


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

    @state.server["channels"][THROUGHPUT].on("bufferedamountlow")
    def on_throughput_buffer_amount_low():
        state.events["throughput_buffer_drained"].set()


def _register_server_package_loss_channel_handler():
    @state.server["channels"][PACKAGE_LOSS].on("message")
    def on_package_loss_message(message):
        state.server["received_packages"] = state.server["received_packages"] + 1


async def server_calculates_client_package_loss():
    #cálculo aqui
    received_packages = state.server["received_packages"]
    lost_packages = 1000 - received_packages
    package_loss = (lost_packages/1000) * 100
    state.server["channels"][CONTROL].send(json.dumps({
                "msg": "package_loss",
                "value": package_loss,
            }))
    state.events["ack_package_loss_received"].clear()
    await event_timeout(state.events["ack_package_loss_received"], PACKAGE_LOSS_TIMEOUT)
    await server_package_loss()


async def server_package_loss():
    state.events["package_loss_received"].clear()
    package = bytes(1)
    for _ in range(1000):
        state.server["channels"][PACKAGE_LOSS].send(package)
    await asyncio.sleep(2)
    state.server["channels"][CONTROL].send(END_PACKAGE_LOSS)
    event_ocurred = await event_timeout(state.events["package_loss_received"], PACKAGE_LOSS_TIMEOUT)
    if not event_ocurred:
        state.results["package_loss"] = None
    save_to_file(state.results)
    state.reset_results()

#acho que nao preciso chamar o calculate_server lataency depois de cada downlaod. EU tenho que chamar quando o ultimo pacote tiver chegando, e eu so sei disso pelo canal de controle
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
        logger.info("<<< recebi LAT")
        state.events["ack_received"].clear()
        state.events["lat_ack_error"].clear()
        await server_send_lat_ack(state.server["channels"][LATENCY])
        await handle_server_latency_timeout(state.server["channels"][CONTROL], LATENCY_TIMEOUT)
    else:  # se ACK for recebido com sucesso
        state.server[state.t1_latency_key()].append(time.time_ns())
        state.events["ack_received"].set()
        logger.info("<<< recebi ACK")
        state.server["channels"][CONTROL].send(END_ITERATION)


def calculate_server_latency(qtd_tests, result_key=LATENCY, test_size=None):
    lat_sum = 0
    qtd_received = min(len(state.server[state.t0_latency_key()]), len(state.server[state.t1_latency_key()]))
    qtd_received = min(qtd_tests, qtd_received)
    valid_packages = 0
    for i in range( qtd_received ) :
        if state.server[state.t1_latency_key()][i] is not None:
            lat_sum = lat_sum + (state.server[state.t1_latency_key()][i] - state.server[state.t0_latency_key()][i])
            valid_packages = valid_packages + 1
        else:
            continue  
        logger.info(f'===> qtd_tests={qtd_tests} \t i={i} of range={qtd_received}')

    col = LATENCY if result_key == LATENCY else f"{THROUGHPUT_LABELS[test_size]}_loaded_latency"
    if valid_packages > 0:
        latency = (lat_sum / valid_packages) / 10**6 #converte de ns para ms
        state.results[col] = round(latency,2)
    else:
        state.results[col] = None
    state.server["channels"][CONTROL].send(END_LATENCY)
    if result_key != LATENCY:
        state.reset_loaded_latency(state.server)
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
