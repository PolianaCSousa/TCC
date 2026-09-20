from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
import asyncio
import os
import time
import logging
import colorlog
import json
from kubo_client import KuboClient
from ipfs_signaling import IpfsSignaling
from swarm_connector import SwarmConnector
from config import (
    get_connection_configuration
)
from custom_types import Client, Server, Peer, Results
from utils import try_parse_json, event_timeout, events_timeout, update_peers_list, safe_send
from storage import save_to_file
from state import state
from constants import (
    CONTROL, LATENCY, THROUGHPUT, PACKAGE_LOSS, HEARTBEAT,
    END_LATENCY, END_THROUGHPUT, END_TEST, START_LOADED_PACKAGES, END_LOADED_PACKAGES, LOADED_LATENCY, LATENCY_PROBE_INTERVAL, LATENCY_TEST_SIZE,
    UPLOAD_RECEIVED, UPLOAD_ERROR, LAT_ACK_ERROR, PACKAGE_LOSS_TIMEOUT,
    LAT, LATENCY_TIMEOUT, LOADED_LATENCY_TIMEOUT,
    MIN_THROUGHPUT_BytePerSec, BYTES_THROUGHPUT_10MB, START_THROUGHPUT,
    BYTES_THROUGHPUT_100KB, BYTES_THROUGHPUT_100MB, BYTES_THROUGHPUT_1MB,
    END_ITERATION, END_LAT_PACKAGES, END_PACKAGE_LOSS, ACK_PACKAGE_LOSS,
    THROUGHPUT_LABELS, BUFFER_AMOUNT_LIMIT, IPFS_TOPIC, CLIENT, SERVER, TEST_INTERVAL_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS, HEARTBEAT_PACKAGE_SIZE,
    ABORTED, RETRY_INTERVAL_SECONDS, ROUND_WATCHDOG_SECONDS
)
from experiments.latency import(
    server_send_lat_ack,
    client_send_lat_package,
    client_send_ack,
    handle_server_latency_timeout,
    calc_latency,
)
from experiments.throughput import(
    send_throughput_data,
    calculate_throughput,
    send_ack_end_upload
)

logger = logging.getLogger(__name__)

# creat a Kubo Client instance and a peer for WebRTC connection
kubo = KuboClient(os.environ.get("KUBO_API", "http://127.0.0.1:5001"))
signaling = IpfsSignaling(kubo,IPFS_TOPIC)
swarm = SwarmConnector(kubo, IPFS_TOPIC)
ice_servers = get_connection_configuration()
peer = None

async def new_peer_connection():
    global peer
    await stop_heartbeat()
    await cancel_round_tasks()
    old_peer, peer = peer, None  # zerar antes de fechar: o statechange do peer antigo passa a ser ignorado
    if old_peer is not None:
        await old_peer.close()
    peer = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
    peer.on("connectionstatechange", _make_state_change_handler(peer))
    # round_active só liga quando a conexão sobe de verdade (ver o handler de estado):
    # um peer esperando par sozinho não tem rodada pra abortar nem resultado pra salvar


# region Round lifecycle
# As etapas da rodada nascem de handlers do pyee, que cria uma task pra cada uma e não
# devolve a referência. Sem rastrear essas tasks, quando a conexão cai elas continuam
# vivas — presas em event_timeout de até 800s — e acordam escrevendo no state da rodada
# SEGUINTE. Por isso toda etapa longa passa por aqui.
_round_tasks: set[asyncio.Task] = set()


def spawn_round_task(coro):
    task = asyncio.create_task(coro)
    _round_tasks.add(task)
    task.add_done_callback(_on_round_task_done)
    return task


def _on_round_task_done(task):
    _round_tasks.discard(task)
    if task.cancelled():
        return
    erro = task.exception()
    if erro is not None:
        # o pyee fazia isso por mim quando o handler era async; agora a task é minha
        logger.error("Etapa da rodada falhou", exc_info=erro)


async def cancel_round_tasks():
    tasks = [task for task in _round_tasks if not task.done()]
    _round_tasks.clear()
    if not tasks:
        return
    logger.info("cancelando %s task(s) da rodada anterior", len(tasks))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def finish_round():
    state.round_active = False
    state.events["round_done"].set()


#A conexão morreu antes do fim. Acorda o loop principal pra reparear.
def abort_round(reason):
    if not state.round_active:
        return  # o par fechou a conexão antiga entre rodadas: é o fluxo normal
    state.round_active = False
    state.results["status"] = ABORTED
    logger.error("Rodada abortada: %s", reason)
    state.events["connection_lost"].set()
# endregion


def get_selected_candidate_pair():
    try:
        connection = peer.sctp.transport.transport._connection
        pair = connection._nominated.get(1)
        if pair is None:
            return None
        local = pair.local_candidate
        return {"local_ip": local.host, "local_type": local.type}
    except (AttributeError, KeyError):
        return None


def _make_state_change_handler(pc):
    # o handler fica preso ao pc que o registrou: sem isso, o peer antigo continuava
    # logando (e reagindo a) o estado do peer novo depois da troca de rodada
    async def on_state_change():
        logger.info("Connection state: %s", pc.connectionState)
        if pc is not peer:
            return
        if pc.connectionState == "connected":
            state.round_active = True  # a partir daqui existe rodada pra abortar
            info = get_selected_candidate_pair()
            if info:
                state.results["ip"] = info["local_ip"]
                state.results["candidate_type"] = info["local_type"]
                logger.info("Candidate local: %s (%s)", info["local_ip"], info["local_type"])
        elif pc.connectionState == "failed":
            logger.error("ICE falhou — nenhum par de candidates funcionou.")
            abort_round("ICE falhou")
        elif pc.connectionState == "closed":
            # o aiortc só chega em "closed" sozinho quando o DTLS morre, e o DTLS morre
            # quando o aioice expira o consent freshness (RFC 7675): 6 binding requests
            # sem resposta, ~30s. Ou seja, o par ficou inalcançável.
            abort_round("conexão fechada (consent freshness do ICE expirou ou o par saiu)")

    return on_state_change


# region Heartbeat
async def _heartbeat_loop(channel):
    package = bytes(HEARTBEAT_PACKAGE_SIZE)
    while channel.readyState == "open":
        if not safe_send(channel, package):
            break
        logger.info("heartbeat enviado (%s bytes)", HEARTBEAT_PACKAGE_SIZE)
        # depois do sleep a conexão pode ter sido fechada, por isso o readyState é
        # checado de novo no topo do loop antes do próximo send
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


def start_heartbeat(channel):
    state.heartbeat_task = asyncio.create_task(_heartbeat_loop(channel))


async def stop_heartbeat():
    task = state.heartbeat_task
    if task is None:
        return
    state.heartbeat_task = None
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
# endregion


# client runs this method to make his offer to peer server
async def client_make_offer(target_name):
    state.role = CLIENT
    state.results["role"] = state.role
    _create_client_data_channels()
    await _create_and_send_sdp_offer(target_name)
    _register_client_control_channel_handlers()
    _register_client_latency_channel_handlers()
    _register_client_throughput_channel_handlers()
    _register_client_package_loss_channel_handlers()
    _register_client_heartbeat_channel_handlers()

signaling.on("role_defined", client_make_offer)


# this method receives the answer from the server peer.
async def client_receives_answer(data):
    logger.info("answer do par servidor recebida no cliente")
    sdp = RTCSessionDescription(sdp=data["answer"]["sdp"], type=data["answer"]["type"])
    await peer.setRemoteDescription(sdp)  # this is the moment the connection is stablished

signaling.on("answer", client_receives_answer)


# region Client methods
def _create_client_data_channels():
    # all channels must be created before the connection stablishment - these channels are created on client peer
    state.client["control_channel"] = peer.createDataChannel(CONTROL)
    state.client["throughput_channel"] = peer.createDataChannel(THROUGHPUT, maxPacketLifeTime=None, maxRetransmits=0,ordered=False)
    state.client["latency_channel"] = peer.createDataChannel(LATENCY)
    state.client["package_loss_channel"] = peer.createDataChannel(PACKAGE_LOSS, maxPacketLifeTime=None, maxRetransmits=0, ordered=False)
    state.client["heartbeat_channel"] = peer.createDataChannel(HEARTBEAT, maxPacketLifeTime=None, maxRetransmits=0, ordered=False)


async def _create_and_send_sdp_offer(target_name):
    offer = await peer.createOffer()
    await peer.setLocalDescription(offer)

    logger.info("oferta criada no par cliente")
    await signaling.send("offer", target_name, {"offer": {"type": peer.localDescription.type, "sdp": peer.localDescription.sdp}})


def _register_client_control_channel_handlers():
    @state.client["control_channel"].on("open")
    async def on_control_open():
        safe_send(state.client["control_channel"], "O teste de LATÊNCIA irá começar...")

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
            safe_send(state.client["control_channel"], ACK_PACKAGE_LOSS)
            logger.info("Perda de pacotes do cliente: %s", state.results["package_loss"])


def _register_client_latency_channel_handlers():
    @state.client["latency_channel"].on("open")
    def on_latency_open():
        spawn_round_task(_client_latency_phase())


    @state.client["latency_channel"].on("message")
    def on_latency_message(message):
        t0 = state.client[state.t0_latency_key()]
        t1 = state.client[state.t1_latency_key()]
        if len(t1) < len(t0):
            t1.append(time.time_ns())
            state.events["lat_ack_received"].set()
      

def _register_client_throughput_channel_handlers():
    @state.client["throughput_channel"].on("open")
    def on_throughput_open():
        spawn_round_task(_client_throughput_phase())


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
    def on_package_loss_open():
        spawn_round_task(_client_package_loss_phase())


    @state.client["package_loss_channel"].on("message")
    def on_package_loss_message(message):
        state.client["received_packages"] = state.client["received_packages"] + 1
        

def _register_client_heartbeat_channel_handlers():
    @state.client["heartbeat_channel"].on("open")
    def on_heartbeat_open():
        start_heartbeat(state.client["heartbeat_channel"])

    @state.client["heartbeat_channel"].on("message")
    def on_heartbeat_message(message):
        pass

# region Client phases
# cada fase roda numa task rastreada (spawn_round_task) pra poder ser cancelada em bloco
# quando a conexão cai — senão elas sobrevivem à rodada e contaminam a próxima
async def _client_latency_phase():
    await client_latency(LATENCY_TEST_SIZE, LATENCY, LATENCY_PROBE_INTERVAL)
    safe_send(state.client["control_channel"], END_LAT_PACKAGES)
    # assim que o cliente termina de enviar ele ja pode calcular sem problema, o que nao pode
    # acontecer é ele começar o teste de vazão antes do servidor terminar de calcular a latência dele
    await calculate_client_latency(LATENCY_TEST_SIZE)


async def _client_throughput_phase():
    # mesmo se o timeout estourar eu posso prosseguir com o teste de vazão
    if not await event_timeout(state.events["latency_finished"], LATENCY_TIMEOUT):
        logger.warning("não recebi END_LATENCY do par em %ss. Começando a vazão assim mesmo.", LATENCY_TIMEOUT)
    safe_send(state.client["control_channel"], START_THROUGHPUT)

    await calculate_client_throughput(BYTES_THROUGHPUT_100KB)
    await calculate_client_throughput(BYTES_THROUGHPUT_1MB)
    await calculate_client_throughput(BYTES_THROUGHPUT_10MB)
    await calculate_client_throughput(BYTES_THROUGHPUT_100MB)
    state.events["end_throughput_experiments"].set()


async def _client_package_loss_phase():
    # espera o fim dos testes de latencia e vazão independentemente do tempo que eles irão gastar
    await state.events["end_throughput_experiments"].wait()
    await client_package_loss()
# endregion


async def calculate_client_throughput(test_size):
    state.reset_for_test()
    state.client["throughput_channel"].bufferedAmountLowThreshold = BUFFER_AMOUNT_LIMIT[test_size]
    await calculate_client_upload(test_size)
    await calculate_client_download(test_size)
    #wait for the test finish completely
    if not await event_timeout(state.events["test_complete"], test_size / MIN_THROUGHPUT_BytePerSec):
        logger.warning("%s: não recebi END_TEST do par em %ss. Seguindo pro próximo tamanho.",
                       THROUGHPUT_LABELS[test_size], test_size / MIN_THROUGHPUT_BytePerSec)


async def calculate_client_upload(test_size):
    # rastreada também: ela é filha desta fase, e cancelar só a mãe deixaria esta viva
    loaded_latency_task = spawn_round_task(client_latency(LATENCY_TEST_SIZE, LOADED_LATENCY, LATENCY_PROBE_INTERVAL, test_size))
    await send_throughput_data(state.client["throughput_channel"], state.client["control_channel"], state.client,test_size)
    ## a task abaixo irá aguardar o evento upload_received ou upload_error
    await send_ack_end_upload(state.client["control_channel"], test_size / MIN_THROUGHPUT_BytePerSec, test_size)
    await loaded_latency_task


async def calculate_client_download(test_size):
    ## a task abaixo irá aguardar o evento throughput_finished
    await calculate_throughput(state.role, state.client, state.events["throughput_finished"], test_size / MIN_THROUGHPUT_BytePerSec)
        

async def client_latency(qtd_tests, type=LATENCY, sleep_loaded_interval=0, test_size=None):
    state.latency_type = "loaded" if (type == LOADED_LATENCY) else "unloaded"
    latency_timeout = LOADED_LATENCY_TIMEOUT if type == LOADED_LATENCY else LATENCY_TIMEOUT
    if type == LOADED_LATENCY:
        safe_send(state.client["control_channel"], START_LOADED_PACKAGES)
    for _ in range(qtd_tests):
        state.events["latency_finished"].clear()
        state.events["loaded_latency_finished"].clear()
        state.events["lat_ack_received"].clear()
        state.events["end_iteration"].clear()
        await client_send_lat_package(state.client["latency_channel"])

        event_occured = await event_timeout(state.events["lat_ack_received"], latency_timeout)
        if event_occured:
            #logger.info("vou enviar o ack pro servidor")
            await client_send_ack(state.client["latency_channel"])
        else:
            state.client[state.t1_latency_key()].append(None) #se o LAT nao chegar no servidor, eu nem vou receber o LAT_ACK, logo meu t1_latency fica sendo None
            safe_send(state.client["control_channel"], LAT_ACK_ERROR)
        #ESPERAR O END_ITERATION
        await event_timeout(state.events["end_iteration"], latency_timeout)

        if sleep_loaded_interval:
            await asyncio.sleep(sleep_loaded_interval)
    
    if type == LOADED_LATENCY:
        safe_send(state.client["control_channel"], END_LOADED_PACKAGES)
        await calculate_client_latency(LATENCY_TEST_SIZE, LOADED_LATENCY, test_size)
        

async def calculate_client_latency(qtd_tests, result_key=LATENCY, test_size=None):
    qtd_received = min(len(state.client[state.t0_latency_key()]), len(state.client[state.t1_latency_key()]))
    qtd_received = min(qtd_tests, qtd_received)
    all_measures = []

    for i in range(qtd_received):
        if state.client[state.t1_latency_key()][i] is not None:
            all_measures.append(state.client[state.t1_latency_key()][i] - state.client[state.t0_latency_key()][i])
        else:
            continue 
        logger.info(f'===> qtd_tests={qtd_tests} \t i={i} of range={qtd_received}')

    calc_latency(all_measures, result_key, test_size)

    if result_key != LATENCY:
        state.reset_loaded_latency(state.client)


async def client_package_loss():
    state.events["package_loss_received"].clear()
    state.events["end_throughput_experiments"].clear()
    package = bytes(1)
    for _ in range(1000):
        if not safe_send(state.client["package_loss_channel"], package):
            break
    await asyncio.sleep(2)
    safe_send(state.client["control_channel"], END_PACKAGE_LOSS)
    event_ocurred = await event_timeout(state.events["package_loss_received"], PACKAGE_LOSS_TIMEOUT)
    if not event_ocurred:
        logger.warning("não recebi a minha perda de pacotes do par em %ss.", PACKAGE_LOSS_TIMEOUT)
        state.results["package_loss"] = None
    save_to_file(state.results)
    state.reset_results()
    finish_round()

def client_calculates_server_package_loss():
    received_packages = state.client["received_packages"]
    lost_packages = 1000 - received_packages
    package_loss = (lost_packages/1000) * 100
    safe_send(state.client["control_channel"], json.dumps({
                "msg": "package_loss",
                "value": package_loss,
            }))
# endregion 

# region Server Receives Offer
async def server_receives_offer(data):
    state.role = SERVER
    state.results["role"] = state.role
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
        elif received_channel.label == HEARTBEAT:
            state.server["channels"][HEARTBEAT] = received_channel
            _register_server_heartbeat_channel_handler()
            start_heartbeat(received_channel)
# endregion
signaling.on("offer", server_receives_offer)

# region Server methods
async def _create_and_send_sdp_answer(data):
    sdp = RTCSessionDescription(sdp=data["offer"]["sdp"], type=data["offer"]["type"])
    await peer.setRemoteDescription(sdp)

    answer = await peer.createAnswer()
    logger.debug("answer criada no server_peer")
    await peer.setLocalDescription(answer)
    await signaling.send("answer", data["from"], {"answer": {"type": peer.localDescription.type, "sdp": peer.localDescription.sdp}})


def _register_server_control_channel_handler():
    @state.server["channels"][CONTROL].on("message")
    async def on_control_message(message):
        logger.debug("[CONTROLE] %s", message)
        msg = try_parse_json(message)
        if message == START_THROUGHPUT: #pode ser que essa mensagem nao chegue, e aí seria um problema, mas o tratamento seria feito no canal webRTC
            spawn_round_task(_calculate_server_download())
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
    def on_latency_message(message):
        #o metodo é chamado sempre que uma mensagem chega nessa canal, logo eu nao posso chamar o calculate_server latency aqui
        spawn_round_task(server_latency(message))


def _register_server_throughput_channel_handler():
    @state.server["channels"][THROUGHPUT].on("message")
    def on_throughput_message(message):
        if state.server["qtd_packages"] == 0:
            state.server["t0_throughput"] = time.time()  # retorna o tempo em segundos
        state.server["qtd_packages"] = state.server["qtd_packages"] + 1

    @state.server["channels"][THROUGHPUT].on("bufferedamountlow")
    def on_throughput_buffer_amount_low():
        state.events["throughput_buffer_drained"].set()


def _register_server_heartbeat_channel_handler():
    @state.server["channels"][HEARTBEAT].on("message")
    def on_heartbeat_message(message):
        pass 


def _register_server_package_loss_channel_handler():
    @state.server["channels"][PACKAGE_LOSS].on("message")
    def on_package_loss_message(message):
        state.server["received_packages"] = state.server["received_packages"] + 1


async def server_calculates_client_package_loss():
    #cálculo aqui
    received_packages = state.server["received_packages"]
    lost_packages = 1000 - received_packages
    package_loss = (lost_packages/1000) * 100
    safe_send(state.server["channels"][CONTROL], json.dumps({
                "msg": "package_loss",
                "value": package_loss,
            }))
    state.events["ack_package_loss_received"].clear()
    if not await event_timeout(state.events["ack_package_loss_received"], PACKAGE_LOSS_TIMEOUT):
        logger.warning("o cliente não confirmou o recebimento da perda de pacotes em %ss.", PACKAGE_LOSS_TIMEOUT)
    await server_package_loss()


async def server_package_loss():
    state.events["package_loss_received"].clear()
    package = bytes(1)
    for _ in range(1000):
        if not safe_send(state.server["channels"][PACKAGE_LOSS], package):
            break
    await asyncio.sleep(2)
    safe_send(state.server["channels"][CONTROL], END_PACKAGE_LOSS)
    event_ocurred = await event_timeout(state.events["package_loss_received"], PACKAGE_LOSS_TIMEOUT)
    if not event_ocurred:
        logger.warning("não recebi a minha perda de pacotes do par em %ss.", PACKAGE_LOSS_TIMEOUT)
        state.results["package_loss"] = None
    save_to_file(state.results)
    state.reset_results()
    finish_round()

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
        latency_timeout = LOADED_LATENCY_TIMEOUT if state.latency_type == "loaded" else LATENCY_TIMEOUT
        await handle_server_latency_timeout(state.server["channels"][CONTROL], latency_timeout)
    else:  # se ACK for recebido com sucesso
        state.server[state.t1_latency_key()].append(time.time_ns())
        state.events["ack_received"].set()
        logger.info("<<< recebi ACK")
        safe_send(state.server["channels"][CONTROL], END_ITERATION)


def calculate_server_latency(qtd_tests, result_key=LATENCY, test_size=None):
    qtd_received = min(len(state.server[state.t0_latency_key()]), len(state.server[state.t1_latency_key()]))
    qtd_received = min(qtd_tests, qtd_received)
    all_measures = []

    for i in range( qtd_received ) :
        if state.server[state.t1_latency_key()][i] is not None:
            all_measures.append((state.server[state.t1_latency_key()][i] - state.server[state.t0_latency_key()][i]))
        else:
            continue  
        logger.info(f'===> qtd_tests={qtd_tests} \t i={i} of range={qtd_received}')

    calc_latency(all_measures, result_key, test_size)

    safe_send(state.server["channels"][CONTROL], END_LATENCY)
    if result_key != LATENCY:
        state.reset_loaded_latency(state.server)
# endregion


def _save_aborted_round():
    """Grava o que a rodada conseguiu medir antes de cair, marcado como incompleto.

    Serve de dado de confiabilidade: dá pra contar quantas rodadas caem e em que fase,
    sem misturar medição truncada com medição válida na análise.
    """
    state.results["status"] = ABORTED
    logger.warning("Salvando rodada incompleta: %s", state.results)
    save_to_file(state.results)
    state.reset_results()


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
    # arquivo de log por peer (nome derivado da porta do Kubo)
    os.makedirs("logs", exist_ok=True)
    porta = kubo.api_url.rsplit(":", 1)[-1]
    file_handler = logging.FileHandler(f"logs/peer_{porta}.log", mode="w")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    # raiz silenciosa (só WARNING+ das libs externas como aiortc/aioice)
    logging.basicConfig(level=logging.WARNING, handlers=[handler, file_handler])
    # libera só o meu módulo
    logger.setLevel(logging.INFO)
    logging.getLogger("experiments").setLevel(logging.INFO)
    logging.getLogger("utils").setLevel(logging.INFO)
    # o aioice loga "Consent to send expired" em INFO. Sem isso, a conexão morre e o
    # único rastro é o "Connection state: closed", sem dizer o motivo.
    logging.getLogger("aioice").setLevel(logging.INFO)


    # Inicializando o Client do Kubo
    await kubo.start()
    await new_peer_connection()
    # Inicializar o signaling (se anunciar no IPFS)
    await signaling.start()
    # o gossipsub só fofoca entre peers já conectados: o swarm connector usa a DHT
    # pra achar os outros participantes e discar neles, senão o announce vai pro vazio
    await swarm.start()

    # loop daemon: roda testes em ciclo, com intervalo entre eles
    try:
        while True:
            # a rodada acaba de três jeitos: terminou, a conexão caiu, ou travou de vez
            outcome = await events_timeout({
                "round_done": state.events["round_done"],
                "connection_lost": state.events["connection_lost"],
            }, ROUND_WATCHDOG_SECONDS)

            if outcome == "round_done":
                espera = TEST_INTERVAL_SECONDS
                logger.info("Rodada concluída. Aguardando %ss até a próxima...", espera)
            elif outcome == "timeout" and not state.round_active:
                # nunca conectou: não há medição nenhuma pra salvar, só tento parear de novo
                logger.warning("Sem par há %ss. Recriando a conexão e repareando.", ROUND_WATCHDOG_SECONDS)
                espera = 0
            else:
                if outcome == "timeout":
                    logger.error("Watchdog: a rodada passou de %ss sem terminar. Abortando.",
                                 ROUND_WATCHDOG_SECONDS)
                    state.round_active = False
                _save_aborted_round()
                espera = RETRY_INTERVAL_SECONDS
                logger.warning("Repareando em %ss.", espera)

            await asyncio.sleep(espera)
            await new_peer_connection()                   # fecha a conexão antiga + cria a nova
            state.reset_for_new_round()                   # limpa acumuladores/latency_type
            signaling.reset_for_new_round()               # volta o signaling pra FREE → repareia
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Saindo...")
    finally:
        await stop_heartbeat()
        await cancel_round_tasks()
        await swarm.close()
        await signaling.close()
        await kubo.close()


if __name__ == "__main__":
    asyncio.run(main())
