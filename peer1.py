from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCDataChannel
import asyncio
import socketio
import time
import json
import config
from custom_types import Client, Server, Peer, Results
from utils import try_parse_json, event_timeout, events_timeout
from storage import save_to_file
from constants import (
    CONTROL, LATENCY, THROUGHPUT,
    END_LATENCY, END_THROUGHPUT, END_TEST,
    UPLOAD_RECEIVED, UPLOAD_ERROR, LAT_ACK_ERROR,
    LAT, LAT_ACK, ACK,
    LATENCY_TIMEOUT, SHORT_TIMEOUT,
    MIN_THROUGHPUT_BytePerSec,
    BYTES_PER_PACKAGE, BYTES_THROUGHPUT_10MB,
)

# creat a Socket.IO client and a peer for WebRTC connection
sio = socketio.AsyncClient()
peer = RTCPeerConnection()


# global variables
CLIENT: Client = {
    "control_channel": None,
    "throughput_channel": None,
    "latency_channel": None,
    "t0_latency": None,
    "t1_latency": None,
    "t0_throughput": None,
    "t1_throughput": None,
    "qtd_packages": 0,
    "qtd_total_bytes": 0
}
SERVER: Server = {
    "channels": {},
    "t0_latency": None,
    "t1_latency": None,
    "t0_throughput": None,
    "t1_throughput": None,
    "qtd_packages": 0,
    "qtd_total_bytes": 0
}

RESULTS: Results = {
    "role": None,
    "sid": None,
    "latency": None,
    "upload": None,
    "download": None
}

SID = None
ROLE = None  # OBS: a client is the peer who makes the offer while the server accepts the offer
# flags for logs
INFO = True
DEBUG = False
SDP = None

events = {
    "lat_ack_received": asyncio.Event(),
    "ack_received": asyncio.Event(),
    "lat_ack_error": asyncio.Event(),
    "upload_error": asyncio.Event(),
    "upload_received": asyncio.Event(),
    "start_server_throughput": asyncio.Event(),
    "latency_finished": asyncio.Event(),
    "throughput_finished": asyncio.Event(),
}


server_peers: list[Peer] = []

# connect to server
@sio.event
async def connect():
    await sio.emit("connected")


# disconnect from server
@sio.event
async def disconnect():
    if INFO:
        print("INFO - Desconectado do servidor")


@sio.on("new_peer")
async def new_peer_on_server(data):
    server_peers.append(data)
    if DEBUG:
        print("DEBUG - lista de pares do servidor: \n")
        print(server_peers)


@sio.on("snapshot")
async def server_snapshot(data):
    global SID
    SID = data["sid"]
    RESULTS["sid"] = SID
    if DEBUG:
        print(f'DEBUG - meu sid é {SID}')
    server_peers.extend(data["snapshot"])

    # after receveing the snapshot from server the peer needs to remove itself from it's local list
    for peer in server_peers:
        if (peer["sid"] == data["sid"]):
            server_peers.remove(peer)
    if DEBUG:
        print(f'DEBUG - snapshot recebido e tratado: {server_peers}')
    await sio.emit("ready_to_start")


@sio.on("role_defined")
async def start_test(data):
    global ROLE
    ROLE = data["role"]
    RESULTS["role"] = ROLE
    if INFO:
        print(f'INFO - sou o {ROLE}')

    if ROLE == "client":
        update_peers_list(data["peer"], 'server')
        if DEBUG:
            print(f'DEBUG - lista local atualizada: {server_peers}')
        target = data["peer"]["target"]
        await client_make_offer(target_name=target)
    else:
        update_peers_list(data["peer"], 'client')
        if DEBUG:
            print(f'DEBUG - lista local atualizada: {server_peers}')


# this method receives the answer from the server peer.
@sio.on("answer")
async def client_receives_answer(data):
    if INFO:
        print("INFO - answer do par servidor recebida no cliente")
    sdp = RTCSessionDescription(sdp=data["answer"]["sdp"], type=data["answer"]["type"])
    await peer.setRemoteDescription(sdp)  # this is the moment the connection is stablished


# region Client Make Offer
# client runs this method to make his offer to peer server
async def client_make_offer(target_name):
    @peer.on("icecandidate")
    def on_icecandidate(candidate):
        print("Client ICE candidate:", candidate)

    # all channels must be created before the connection stablishment - these channels are created on client peer
    CLIENT["control_channel"] = peer.createDataChannel(CONTROL)
    CLIENT["throughput_channel"] = peer.createDataChannel(THROUGHPUT, maxPacketLifeTime=None, maxRetransmits=0,
                                                          ordered=False)
    CLIENT["latency_channel"] = peer.createDataChannel(LATENCY)

    # region Create SDP offer
    offer = await peer.createOffer()
    await peer.setLocalDescription(offer)

    if INFO:
        print("INFO - oferta criada no par cliente")
    # print(f'debug - sdp do peer: {peer.localDescription.sdp}')
    await sio.emit("offer", {
        "to": target_name,
        "offer": {
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp
        }
    })

    # endregion

    @CLIENT["control_channel"].on("open")
    async def control_task():
        CLIENT["control_channel"].send("O teste de LATÊNCIA irá começar...")

    @CLIENT["latency_channel"].on("open")
    async def on_latency_channel():
        events["latency_finished"].clear()
        events["lat_ack_received"].clear()
        asyncio.create_task(client_send_lat_package(CLIENT["latency_channel"]))
        event_occured = await event_timeout(events["lat_ack_received"], LATENCY_TIMEOUT)
        if event_occured:
            if INFO:
                print(f'INFO - vou enviar o ack pro servidor')
            asyncio.create_task(client_send_ack(CLIENT["latency_channel"]))
            events["lat_ack_received"].clear()
        else:
            RESULTS["latency"] = None  # eu so mantenho a latência do cliente vazia - nesse caso, a latencia do servidor tambem vai ser none
            # ja que o timeout estourou de ca, preciso avisar o outro par que deu errado pra ele nao ficar esperando atoa
            # portanto, pra avisar ao servidor que eu nao recebi o lat-ack e portanto ele nao vai nem receber o ack, vou mandar uma mensagem de erro no canal de controle pra ele tratar de lá
            CLIENT["control_channel"].send(LAT_ACK_ERROR)

    @CLIENT["throughput_channel"].on("open")
    async def on_throughput_channel():
        event_occured = await event_timeout(events["latency_finished"],
                                            LATENCY_TIMEOUT)  # nesse caso, mesmo se o timeout estourar, eu posso prosseguir com o teste de vazão
        if event_occured:
            if INFO:
                print("INFO - latência finalizada. Vazão vai começar")
            CLIENT["control_channel"].send("O teste de VAZÃO irá começar...")
        else:
            CLIENT["control_channel"].send(
                "O teste de VAZÃO irá começar, apesar do cliente não ter recebido o pacote de fim de latência do par servidor.")
        asyncio.create_task(send_throughput_data(CLIENT["throughput_channel"], CLIENT["control_channel"], CLIENT,
                                                 BYTES_THROUGHPUT_10MB))  # TESTE COM 10MB por enquanto

        ## a task abaixo irá aguardar o evento upload_received ou upload_error
        bytes_to_be_sent = BYTES_THROUGHPUT_10MB
        asyncio.create_task(
            send_ack_end_upload(CLIENT["control_channel"], bytes_to_be_sent / MIN_THROUGHPUT_BytePerSec))

        ## a task abaixo irá aguardar o evento throughput_finished
        asyncio.create_task(
            calculate_throughput(ROLE, CLIENT, events["throughput_finished"], bytes_to_be_sent / MIN_THROUGHPUT_BytePerSec))

    @CLIENT["latency_channel"].on("message")
    def on_latency_message(message):
        CLIENT["t1_latency"] = time.time_ns()
        events[
            "lat_ack_received"].set()  # adicionei o set do evento depois de enviar o ack pra poder enviar o ack o mais rapido possivel de volta pro par servidor - CONFIRMAR COM EVERTHON
        calc_latency_a_b = (CLIENT["t1_latency"] - CLIENT["t0_latency"]) / (10 ** 6)

        if ROLE == "client":
            RESULTS["latency"] = calc_latency_a_b

        if INFO:
            print("INFO - <<< recebi LAT-ACK")
            print(f'INFO - LATÊNCIA DO CLIENTE a=>b {calc_latency_a_b} ms')

        # CLIENT["control_channel"].send(f'LATÊNCIA a=>b {calc_latency_a_b} ms')

    @CLIENT["control_channel"].on("message")
    async def on_control_message(message):
        msg = try_parse_json(message)
        if DEBUG:
            print(f'upload (json): {msg}')
        elif message == END_LATENCY:
            if INFO:
                print("INFO - cliente recebeu END_LATENCY")
            events["latency_finished"].set()
        elif message == END_THROUGHPUT:
            events["throughput_finished"].set()
        elif message == UPLOAD_ERROR:
            events["upload_error"].set()
        elif message == END_TEST:
            print("primeiro teste finalizado")
        elif msg is not None:
            RESULTS["upload"] = msg["value"]
            events["upload_received"].set()
        #else:
            #print(f"[CONTROLE]\t {message}")

    @CLIENT["throughput_channel"].on("message")
    def on_throughput_message(message):
        if CLIENT["qtd_packages"] == 0:
            CLIENT["t0_throughput"] = time.time()  # retorna o tempo em segundos
            # print(f'debug - tamanho do pacote recebido {len(message)}') #sys.getsizeof(package) retorna o tamanho do objeto Python na memória
        CLIENT["qtd_packages"] = CLIENT["qtd_packages"] + 1


# endregion

# region Server Receives Offer

@sio.on("offer")
async def server_receives_offer(data):
    if DEBUG:
        print("DEBUG - offer recebida no server_peer")

    # region Cria resposta SDP
    sdp = RTCSessionDescription(sdp=data["offer"]["sdp"], type=data["offer"]["type"])
    await peer.setRemoteDescription(sdp)

    answer = await peer.createAnswer()
    if DEBUG:
        print("DEBUG - answer criada no server_peer")
    await peer.setLocalDescription(answer)

    await sio.emit("answer", {
        "to": data["from"],  # data["from"] é o sid do client_peer OK
        "answer": {
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp
        }
    })

    # endregion

    @peer.on("datachannel")
    def on_datachannel(received_channel):
        if (received_channel.label == CONTROL):
            SERVER["channels"][CONTROL] = received_channel

            @received_channel.on("message")
            async def on_control_message(message):
                print(f'[CONTROLE] {message}')
                msg = try_parse_json(message)
                if message == END_THROUGHPUT:
                    events["throughput_finished"].set()
                if message == UPLOAD_RECEIVED:
                    events["start_server_throughput"].set()
                if message == UPLOAD_ERROR:
                    events["upload_error"].set()
                if message == LAT_ACK_ERROR:
                    events["lat_ack_error"].set()
                if msg is not None:
                    RESULTS["upload"] = msg["value"]  # It's here when the tests finish for server
                    save_to_file(RESULTS)
                    events["upload_received"].set()  # aqui preciso tratar
                    if INFO:
                        print(f'INFO - Resultados do servidor: {RESULTS}')
                if DEBUG:
                    print(f'upload (json): {msg}')

        if received_channel.label == LATENCY:
            SERVER["channels"][LATENCY] = received_channel

            @received_channel.on("message")
            def on_latency_message(message):
                if message == LAT:
                    events["ack_received"].clear()
                    events["lat_ack_error"].clear()
                    asyncio.create_task(server_send_lat_ack(received_channel))
                    asyncio.create_task(handle_server_latency_timeout(SERVER["channels"][CONTROL], LATENCY_TIMEOUT))
                    if INFO:
                        print("INFO - <<< recebi LAT")
                else:  # se ACK for recebido com sucesso
                    SERVER["t1_latency"] = time.time_ns()
                    events["ack_received"].set()
                    calc_latency_b_a = (SERVER["t1_latency"] - SERVER["t0_latency"]) / (10 ** 6)

                    if ROLE == 'server':
                        RESULTS[LATENCY] = calc_latency_b_a

                    if INFO:
                        print("INFO - <<< recebi ACK")
                        print(f"INFO - LATÊNCIA DO SERVIDOR b=>a {calc_latency_b_a} ms")

                    # SERVER["channels"][CONTROL].send(f'LATÊNCIA b=>a {calc_latency_b_a} ms')
                    SERVER["channels"][CONTROL].send(END_LATENCY)

        if received_channel.label == THROUGHPUT:
            SERVER["channels"][THROUGHPUT] = received_channel
            # TESTE SÓ PRA VER SE CORRIGE MESMO
            SERVER["qtd_total_bytes"] = BYTES_THROUGHPUT_10MB
            # a task abaixo espera o envio dos dados terminar - throughput_finished
            asyncio.create_task(calculate_throughput(ROLE, SERVER, events["throughput_finished"]))

            @received_channel.on("message")
            def on_throughput_message(message):
                if SERVER["qtd_packages"] == 0:
                    SERVER["t0_throughput"] = time.time()  # retorna o tempo em segundos
                SERVER["qtd_packages"] = SERVER["qtd_packages"] + 1


# endregion

async def send_throughput_data(throughput_channel, control_channel, PEER, test_size):
    try:
        package = bytes(BYTES_PER_PACKAGE)
        # as duas linhas a seguir podem virar so uma
        # tam_total_dados = BYTES_THROUGHPUT_10MB  # enviarei no total 10MB = 10.000.000 Bytes
        PEER["qtd_total_bytes"] = test_size
        qtd_pacotes = test_size // len(package)
        tam_pacote = len(package)
        if DEBUG:
            print(
                f'DEBUG - o envios dos pacotes vai começar agora. \nVou enviar {qtd_pacotes} pacotes de tamanho {tam_pacote}')
            print("DEBUG - ICE:", peer.iceConnectionState)
            print("DEBUG - DTLS:", peer.connectionState)
        for i in range(0, qtd_pacotes):
            throughput_channel.send(package)
        # throughput_channel.send(END_THROUGHPUT)
        control_channel.send(END_THROUGHPUT)
    except Exception as e:
        print(f'Erro no envio dos dados da vazão: {e}')


async def calculate_throughput(ROLE, PEER, throughput_finished, timeout=5):
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
        if ROLE == "server":
            RESULTS["download"] = vazao_em_Mbps
            # SERVER["channels"][CONTROL].send(f'RESULTADO DO TESTE DE UPLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
            SERVER["channels"][CONTROL].send(json.dumps({
                "msg": "upload",
                "value": vazao_em_Mbps
            }))
            print(f'INFO - RESULTADO DO TESTE DE DOWNLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
            response = await event_timeout(events["start_server_throughput"], SHORT_TIMEOUT)
            if response:
                SERVER["channels"][CONTROL].send(
                    "Não recebi o ACK do resultado do upload do cliente. Vou iniciar o teste mesmo assim.")
            else:
                SERVER["channels"][CONTROL].send("Recebi ACK do upload do cliente. Vou iniciar o teste agora.")
            asyncio.create_task(send_throughput_data(SERVER["channels"][THROUGHPUT], SERVER["channels"][CONTROL], SERVER,
                                                     BYTES_THROUGHPUT_10MB))  # TESTE COM 10MB por enquanto
            ## a task abaixo irá aguardar o evento upload_received ou upload_error
            bytes_to_be_sent = BYTES_THROUGHPUT_10MB
            asyncio.create_task(send_end_test(SERVER["channels"][CONTROL], bytes_to_be_sent / MIN_THROUGHPUT_BytePerSec))
        else:
            if DEBUG:
                print(f'sou cliente e ja tenho o download: {vazao_em_Mbps} Mbps')
            RESULTS["download"] = vazao_em_Mbps  # It's here when the tests finish for client
            print(f'INFO - Resultados do cliente: {RESULTS}')
            save_to_file(RESULTS)
            CLIENT["control_channel"].send(
                f'RESULTADO DO TESTE DE UPLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
            CLIENT["control_channel"].send(json.dumps({
                "msg": "upload",
                "value": vazao_em_Mbps
            }))
            print(f'INFO - RESULTADO DO TESTE DE DOWNLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
    else:
        # meu download é none e o do outro par é none o upload
        RESULTS["download"] = None
        if ROLE == "server":
            SERVER["channels"][CONTROL].send(UPLOAD_ERROR)
        else:
            CLIENT["control_channel"].send(UPLOAD_ERROR)


# region Calculate and send latency package
async def server_send_lat_ack(latency_channel):
    SERVER["t0_latency"] = time.time_ns()
    latency_channel.send(LAT_ACK)
    if INFO:
        print("INFO - >>> enviei LAT_ACK")


# endregion

async def client_send_lat_package(latency_channel):
    CLIENT["t0_latency"] = time.time_ns()
    latency_channel.send(LAT)
    if INFO:
        print("INFO - >>> enviei LAT")


async def client_send_ack(latency_channel):
    latency_channel.send(ACK)
    if INFO:
        print("INFO - >>> enviei ACK")


def update_peers_list(this, role):
    for peer in server_peers:
        if peer["sid"] == this["target"]:
            peer["role"] = role
            peer["status"] = "OCCUPIED"
            peer["target"] = SID


async def send_ack_end_upload(control_channel, timeout):
    response = await events_timeout({"upload_received": events["upload_received"],
                                     "upload_error": events["upload_error"]
                                     }, timeout)
    if response == "upload_received":
        control_channel.send(UPLOAD_RECEIVED)
    else:
        if RESULTS["upload"] is not None:
            RESULTS["upload"] = None
        if ROLE == "client":
            control_channel.send(UPLOAD_RECEIVED)  # vou enviar mesmo que tenha dado errado pra que o teste continue
        else:
            control_channel.send(UPLOAD_ERROR)


async def send_end_test(control_channel, timeout):
    response = await events_timeout({"upload_received": events["upload_received"],
                                     "upload_error": events["upload_error"]
                                     }, timeout)
    if response == "upload_error" and RESULTS["upload"] is not None:
        RESULTS["upload"] = None
    control_channel.send(END_TEST)  # somente aqui eu envio o fim do teste, quando da certo ou quando da errado


async def handle_server_latency_timeout(control_channel, timeout):
    # event_ocurred = await event_timeout
    response = await events_timeout({"ack_received": events["ack_received"],
                                     "lat_ack_error": events["lat_ack_error"]
                                     }, timeout)
    if response != "ack_received":
        if not RESULTS[LATENCY]:
            # RESULTS[LATENCY] = None
            control_channel.send(END_LATENCY)


# Função principal para iniciar o cliente e conectar
async def main():
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
