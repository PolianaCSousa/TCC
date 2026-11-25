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
    get_connection_configuration,
    relax_ice_consent
)
from custom_types import Client, Server, Peer, Results
from utils import try_parse_json, event_timeout, events_timeout, update_peers_list, safe_send, wait_round_outcome, send_paced
from storage import save_to_file
from state import state
from constants import (
    CONTROL, LATENCY, THROUGHPUT, PACKAGE_LOSS, HEARTBEAT,
    END_LATENCY, END_THROUGHPUT, END_TEST, START_LOADED_PACKAGES, END_LOADED_PACKAGES, LOADED_LATENCY, LATENCY_PROBE_INTERVAL, LATENCY_TEST_SIZE,
    UPLOAD_RECEIVED, UPLOAD_ERROR, SEND_ABORTED, LAT_ACK_ERROR, PACKAGE_LOSS_TIMEOUT,
    LAT, LATENCY_TIMEOUT, LOADED_LATENCY_TIMEOUT,
    MIN_THROUGHPUT_BytePerSec, BYTES_THROUGHPUT_10MB, START_THROUGHPUT,
    BYTES_THROUGHPUT_100KB, BYTES_THROUGHPUT_50MB, BYTES_THROUGHPUT_1MB,
    END_ITERATION, END_LAT_PACKAGES, END_PACKAGE_LOSS, ACK_PACKAGE_LOSS,
    PACKAGE_LOSS_TOTAL, PACKAGE_LOSS_BATCH, PACKAGE_LOSS_PAUSE,
    THROUGHPUT_LABELS, BUFFER_AMOUNT_LIMIT, IPFS_TOPIC, CLIENT, SERVER, TEST_INTERVAL_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS, HEARTBEAT_PACKAGE_SIZE,
    ABORTED, RETRY_INTERVAL_SECONDS, ROUND_WATCHDOG_SECONDS, PAIRING_TIMEOUT_SECONDS,
    REPLY_TIMEOUT_SECONDS, SPEEDTEST_DELAY_SECONDS
)
from speedtest_runner import run_speedtest
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
    send_ack_end_upload,
    abort_upload
)

logger = logging.getLogger(__name__)

kubo = KuboClient(os.environ.get("KUBO_API", "http://127.0.0.1:5001"))
signaling = IpfsSignaling(kubo,IPFS_TOPIC)
swarm = SwarmConnector(kubo, IPFS_TOPIC)
ice_servers = get_connection_configuration()
peer = None

async def new_peer_connection():
    global peer
    await stop_heartbeat()
    await cancel_round_tasks()
    old_peer, peer = peer, None  # cleared before closing: the old peer's handler is then ignored
    if old_peer is not None:
        await old_peer.close()
    peer = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
    peer.on("connectionstatechange", _make_state_change_handler(peer))


# region Round lifecycle
# round tasks are tracked so they can be cancelled when the connection drops
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
    logger.info("---------------- FIM DO EXPERIMENTO ----------------")
    state.round_active = False
    state.events["round_done"].set()


# endregion


def get_selected_candidate_pair():
    try:
        connection = peer.sctp.transport.transport._connection
        pair = connection._nominated.get(1)
        if pair is None:
            return None
        local = pair.local_candidate
        remote = pair.remote_candidate
        remote_type = _remote_type_from_sdp(connection, remote)
        logger.debug("ICE escolheu: local %s:%s (%s) -> remoto %s:%s (%s pelo ICE, %s pelo SDP)",
                    local.host, local.port, local.type,
                    remote.host, remote.port, remote.type, remote_type)
        return {"local_ip": local.host, "local_type": local.type, "remote_type": remote_type}
    except (AttributeError, KeyError):
        return None


# remote candidate type as declared in the SDP (aioice labels as "prflx" whatever arrives before it)
def _remote_type_from_sdp(connection, remote):
    do_sdp = [c for c in connection._remote_candidates if c.type != "prflx"]
    for c in do_sdp:
        logger.debug("SDP do par: %s:%s typ %s", c.host, c.port, c.type)
    for c in do_sdp:
        if (c.host, c.port) == (remote.host, remote.port):
            return c.type
    return remote.type


def _make_state_change_handler(pc):
    # handler bound to the pc that registered it: events from an old connection are ignored
    async def on_state_change():
        logger.info("Connection state: %s", pc.connectionState)
        if pc is not peer:
            return
        if pc.connectionState == "connected":
            state.round_active = True
            info = get_selected_candidate_pair()
            if info:
                state.results["ip"] = info["local_ip"]
                state.results["candidate_type"] = info["local_type"]
                state.results["remote_candidate_type"] = info["remote_type"]
                # string, not bool: it becomes an influx tag
                state.results["via_turn"] = str("relay" in (info["local_type"], info["remote_type"])).lower()
                logger.info("Candidate local: %s (%s)", info["local_ip"], info["local_type"])
        elif pc.connectionState == "failed":
            logger.error("ICE falhou — nenhum par de candidates funcionou.")
            state.abort_round("ICE falhou")
        elif pc.connectionState == "closed":
            # aiortc only closes on its own when ICE consent freshness expires (peer unreachable)
            state.abort_round("conexão fechada (consent freshness do ICE expirou ou o par saiu)")

    return on_state_change


# region Heartbeat
async def _heartbeat_loop(channel):
    package = bytes(HEARTBEAT_PACKAGE_SIZE)
    while channel.readyState == "open":
        if not safe_send(channel, package):
            break
        logger.debug("heartbeat enviado (%s bytes)", HEARTBEAT_PACKAGE_SIZE)
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


async def client_make_offer(target_name):
    state.role = CLIENT
    state.results["role"] = state.role
    state.results["peer_id"] = signaling.my_id
    state.results["remote_peer_id"] = target_name
    _create_client_data_channels()
    await _create_and_send_sdp_offer(target_name)
    _register_client_control_channel_handlers()
    _register_client_latency_channel_handlers()
    _register_client_throughput_channel_handlers()
    _register_client_package_loss_channel_handlers()
    _register_client_heartbeat_channel_handlers()

signaling.on("role_defined", client_make_offer)


async def client_receives_answer(data):
    logger.info("answer do par servidor recebida no cliente")
    sdp = RTCSessionDescription(sdp=data["answer"]["sdp"], type=data["answer"]["type"])
    await peer.setRemoteDescription(sdp)

signaling.on("answer", client_receives_answer)


# region Client methods
def _create_client_data_channels():
    # channels must be created before the offer
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
        if message == END_LATENCY:
            logger.info("cliente recebeu END_LATENCY")
            state.events["latency_finished"].set()
        elif message == END_ITERATION:
            state.events["end_iteration"].set()
        elif message == END_THROUGHPUT:
            # t1 is recorded when the message arrives, not when the event is consumed
            state.client["t1_throughput"] = time.monotonic()
            state.events["throughput_finished"].set()
        elif message == UPLOAD_ERROR:
            state.events["upload_error"].set()
        elif message == SEND_ABORTED:
            state.events["send_aborted"].set()
        elif message == END_TEST:
            logger.info("------ TESTE FINALIZADO ------")
            state.events["test_complete"].set()
        elif message == END_PACKAGE_LOSS:
            client_calculates_server_package_loss()
        elif msg is not None and msg["msg"] == 'upload':
            logger.debug("Upload do cliente: %s", msg["value"])
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
            t1.append(time.monotonic_ns())
            state.events["lat_ack_received"].set()
      

def _register_client_throughput_channel_handlers():
    @state.client["throughput_channel"].on("open")
    def on_throughput_open():
        spawn_round_task(_client_throughput_phase())


    @state.client["throughput_channel"].on("message")
    def on_throughput_message(message):
        if state.client["qtd_packages"] == 0:
            state.client["t0_throughput"] = time.monotonic()
            state.client["t1_throughput"] = None
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
async def _client_latency_phase():
    await client_latency(LATENCY_TEST_SIZE, LATENCY, LATENCY_PROBE_INTERVAL)
    safe_send(state.client["control_channel"], END_LAT_PACKAGES)
    await calculate_client_latency(LATENCY_TEST_SIZE)


async def _client_throughput_phase():
    # the throughput test can proceed even if the timeout expires
    if not await event_timeout(state.events["latency_finished"], LATENCY_TIMEOUT):
        logger.warning("não recebi END_LATENCY do par em %ss. Começando a vazão assim mesmo.", LATENCY_TIMEOUT)
    safe_send(state.client["control_channel"], START_THROUGHPUT)

    await calculate_client_throughput(BYTES_THROUGHPUT_100KB)
    await calculate_client_throughput(BYTES_THROUGHPUT_1MB)
    await calculate_client_throughput(BYTES_THROUGHPUT_10MB)
    await calculate_client_throughput(BYTES_THROUGHPUT_50MB)
    state.events["end_throughput_experiments"].set()


async def _client_package_loss_phase():
    # waits for the latency and throughput tests to finish
    await state.events["end_throughput_experiments"].wait()
    await client_package_loss()
# endregion


async def calculate_client_throughput(test_size):
    state.reset_for_test()
    state.client["throughput_channel"].bufferedAmountLowThreshold = BUFFER_AMOUNT_LIMIT[test_size]
    await calculate_client_upload(test_size)
    await calculate_client_download(test_size)
    if not await event_timeout(state.events["test_complete"], REPLY_TIMEOUT_SECONDS):
        logger.warning("%s: não recebi END_TEST do par em %ss. Seguindo pro próximo tamanho.",
                       THROUGHPUT_LABELS[test_size], REPLY_TIMEOUT_SECONDS)


async def calculate_client_upload(test_size):
    loaded_latency_task = spawn_round_task(client_latency(LATENCY_TEST_SIZE, LOADED_LATENCY, LATENCY_PROBE_INTERVAL, test_size))
    enviou = await send_throughput_data(state.client["throughput_channel"], state.client["control_channel"], state.client,test_size)
    if enviou:
        # waits for the upload_received or upload_error event
        await send_ack_end_upload(state.client["control_channel"], REPLY_TIMEOUT_SECONDS, test_size)
    else:
        abort_upload(state.client["control_channel"], test_size)
    await loaded_latency_task


async def calculate_client_download(test_size):
    # waits for the throughput_finished event
    await calculate_throughput(state.role, state.client, state.events["throughput_finished"])
        

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
            await client_send_ack(state.client["latency_channel"])
        else:
            state.client[state.t1_latency_key()].append(None) # without LAT_ACK, this probe's t1 stays None
            safe_send(state.client["control_channel"], LAT_ACK_ERROR)
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
        logger.debug("===> qtd_tests=%s \t i=%s of range=%s", qtd_tests, i, qtd_received)

    calc_latency(all_measures, result_key, test_size)

    if result_key != LATENCY:
        state.reset_loaded_latency(state.client)


async def client_package_loss():
    state.events["package_loss_received"].clear()
    state.events["end_throughput_experiments"].clear()
    enviados = await send_paced(state.client["package_loss_channel"], bytes(1),
                                PACKAGE_LOSS_TOTAL, PACKAGE_LOSS_BATCH, PACKAGE_LOSS_PAUSE)
    if enviados < PACKAGE_LOSS_TOTAL:
        logger.warning("perda: só %s de %s pacotes saíram (canal fechou).",
                       enviados, PACKAGE_LOSS_TOTAL)
    await asyncio.sleep(2)   # lets in-flight packets arrive before counting
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
    state.results["peer_id"] = signaling.my_id
    state.results["remote_peer_id"] = data["from"]
    logger.info("offer recebida no server_peer")
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
    logger.info("answer criada no server_peer")
    await peer.setLocalDescription(answer)
    await signaling.send("answer", data["from"], {"answer": {"type": peer.localDescription.type, "sdp": peer.localDescription.sdp}})


def _register_server_control_channel_handler():
    @state.server["channels"][CONTROL].on("message")
    async def on_control_message(message):
        logger.debug("[CONTROLE] %s", message)
        msg = try_parse_json(message)
        if message == START_THROUGHPUT:
            spawn_round_task(_calculate_server_download())
        elif message == END_LAT_PACKAGES:
            calculate_server_latency(LATENCY_TEST_SIZE)
        elif message == START_LOADED_PACKAGES:
            state.latency_type = "loaded"
        elif message == END_LOADED_PACKAGES:
            calculate_server_latency(LATENCY_TEST_SIZE, LOADED_LATENCY, state.server["qtd_total_bytes"])
        elif message == END_THROUGHPUT:
            # t1 is recorded when the message arrives, not when the event is consumed
            state.server["t1_throughput"] = time.monotonic()
            state.events["throughput_finished"].set()
        elif message == UPLOAD_RECEIVED:
            state.events["start_server_upload"].set()
        elif message == UPLOAD_ERROR:
            state.events["upload_error"].set()
        elif message == SEND_ABORTED:
            state.events["send_aborted"].set()
        elif message == LAT_ACK_ERROR:
            state.events["lat_ack_error"].set()
        elif message == END_PACKAGE_LOSS:
            await server_calculates_client_package_loss()
        elif message == ACK_PACKAGE_LOSS:
            state.events["ack_package_loss_received"].set()
        elif msg is not None and msg["msg"] == 'upload':
            logger.debug("Upload do servidor: %s", msg["value"])
            label = THROUGHPUT_LABELS[msg["test_size"]]
            state.results[f"{label}_upload"] = msg["value"]
            state.events["upload_received"].set()
            logger.debug("Resultados do servidor: %s", state.results)
        elif msg is not None and msg["msg"] == 'package_loss':
            state.results["package_loss"] = msg["value"]
            state.events["package_loss_received"].set()
            logger.info("Perda de pacotes do servidor: %s", state.results["package_loss"])


def _register_server_latency_channel_handler():
    @state.server["channels"][LATENCY].on("message")
    def on_latency_message(message):
        spawn_round_task(server_latency(message))


def _register_server_throughput_channel_handler():
    @state.server["channels"][THROUGHPUT].on("message")
    def on_throughput_message(message):
        if state.server["qtd_packages"] == 0:
            state.server["t0_throughput"] = time.monotonic()
            state.server["t1_throughput"] = None
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
    enviados = await send_paced(state.server["channels"][PACKAGE_LOSS], bytes(1),
                                PACKAGE_LOSS_TOTAL, PACKAGE_LOSS_BATCH, PACKAGE_LOSS_PAUSE)
    if enviados < PACKAGE_LOSS_TOTAL:
        logger.warning("perda: só %s de %s pacotes saíram (canal fechou).",
                       enviados, PACKAGE_LOSS_TOTAL)
    await asyncio.sleep(2)   # lets in-flight packets arrive before counting
    safe_send(state.server["channels"][CONTROL], END_PACKAGE_LOSS)
    event_ocurred = await event_timeout(state.events["package_loss_received"], PACKAGE_LOSS_TIMEOUT)
    if not event_ocurred:
        logger.warning("não recebi a minha perda de pacotes do par em %ss.", PACKAGE_LOSS_TIMEOUT)
        state.results["package_loss"] = None
    save_to_file(state.results)
    state.reset_results()
    finish_round()

async def _calculate_server_download():
    await _calculate_download(BYTES_THROUGHPUT_100KB)
    await _calculate_download(BYTES_THROUGHPUT_1MB)
    await _calculate_download(BYTES_THROUGHPUT_10MB)
    await _calculate_download(BYTES_THROUGHPUT_50MB)


async def _calculate_download(test_size):
    state.reset_for_test()
    state.server["qtd_total_bytes"] = test_size
    # waits for the client's send to finish (throughput_finished)
    await calculate_throughput(state.role, state.server, state.events["throughput_finished"])


async def server_latency(message):
    if message == LAT:
        logger.debug("<<< recebi LAT")
        state.events["ack_received"].clear()
        state.events["lat_ack_error"].clear()
        await server_send_lat_ack(state.server["channels"][LATENCY])
        latency_timeout = LOADED_LATENCY_TIMEOUT if state.latency_type == "loaded" else LATENCY_TIMEOUT
        await handle_server_latency_timeout(state.server["channels"][CONTROL], latency_timeout)
    else:  # ACK received
        # appends t1 only if a t0 is unpaired, to keep the indexes aligned
        t0_srv = state.server[state.t0_latency_key()]
        t1_srv = state.server[state.t1_latency_key()]
        if len(t1_srv) < len(t0_srv):
            t1_srv.append(time.monotonic_ns())
        else:
            logger.warning("ACK fora de par (t0=%s t1=%s): ignorando pra não desalinhar.",
                           len(t0_srv), len(t1_srv))
        state.events["ack_received"].set()
        logger.debug("<<< recebi ACK")
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
        logger.debug("===> qtd_tests=%s \t i=%s of range=%s", qtd_tests, i, qtd_received)

    calc_latency(all_measures, result_key, test_size)

    safe_send(state.server["channels"][CONTROL], END_LATENCY)
    if result_key != LATENCY:
        state.reset_loaded_latency(state.server)
# endregion


# saves what the round measured before dropping, marked as aborted
def _save_aborted_round():
    state.results["status"] = ABORTED
    logger.warning("Salvando rodada incompleta: %s", state.results)
    save_to_file(state.results)
    state.reset_results()


# demotes to DEBUG the aioice "Check CandidatePair(...)" logs, which are emitted at INFO
def _ice_checks_em_debug(record):
    if isinstance(record.msg, str) and record.msg.startswith("%s Check "):
        record.levelno, record.levelname = logging.DEBUG, "DEBUG"
        return logging.getLogger("aioice.ice").isEnabledFor(logging.DEBUG)
    return True


async def main():
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
    # one log file per peer (named after the Kubo port)
    os.makedirs("logs", exist_ok=True)
    porta = kubo.api_url.rsplit(":", 1)[-1]
    file_handler = logging.FileHandler(f"logs/peer_{porta}.log", mode="w")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    # root at WARNING: silences external libraries
    logging.basicConfig(level=logging.WARNING, handlers=[handler, file_handler])
    # INFO only for the tool's own modules
    logger.setLevel(logging.INFO)
    logging.getLogger("experiments").setLevel(logging.INFO)
    logging.getLogger("utils").setLevel(logging.INFO)
    logging.getLogger("config").setLevel(logging.INFO)
    # aioice logs "Consent to send expired" at INFO
    logging.getLogger("aioice").setLevel(logging.INFO)
    logging.getLogger("aioice.ice").addFilter(_ice_checks_em_debug)
    logging.getLogger("ipfs_signaling").setLevel(logging.INFO)
    logging.getLogger("swarm_connector").setLevel(logging.INFO)
    logging.getLogger("speedtest_runner").setLevel(logging.INFO)

    # must be in effect before the first connection
    relax_ice_consent()

    await kubo.start()
    await new_peer_connection()
    await signaling.start()
    await swarm.start()

    # daemon loop: runs tests in a cycle, with an interval between them
    try:
        while True:
            outcome = await wait_round_outcome()

            if outcome == "round_done":
                logger.info("Rodada concluída. Speedtest em %ss...", SPEEDTEST_DELAY_SECONDS)
                await asyncio.sleep(SPEEDTEST_DELAY_SECONDS)
                await run_speedtest(state.role)
                espera = TEST_INTERVAL_SECONDS
                logger.info("Aguardando %ss até a próxima rodada...", espera)
            elif outcome == "timeout" and not state.round_active:
                # never connected: nothing to save, just re-pair
                logger.warning("Sem par há %ss. Recriando a conexão e repareando.", PAIRING_TIMEOUT_SECONDS)
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
            await new_peer_connection()
            state.reset_for_new_round()
            signaling.reset_for_new_round()
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
