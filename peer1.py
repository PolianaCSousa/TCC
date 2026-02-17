from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCDataChannel
import asyncio
import socketio
import time
from typing import TypedDict
import json
import pandas as pd
import os

#creat a Socket.IO client and a peer for WebRTC connection
sio = socketio.AsyncClient()
peer = RTCPeerConnection()

#typing for global CLIENT and SERVER dictionaries
class Client(TypedDict):
    control_channel: RTCDataChannel | None
    throughput_channel: RTCDataChannel | None
    latency_channel: RTCDataChannel | None
    t0_latency: int | None
    t1_latency: int | None
    t0_throughput: int | float | None
    t1_throughput: int | float | None
    qtd_packages: int

class Server(TypedDict):
    t0_latency: int | None
    t1_latency: int | None
    t0_throughput: int | float | None
    t1_throughput: int | float | None
    qtd_packages: int

class Peer(TypedDict):
    role: str | None
    target: str | None
    status: str | None
    sid: str | None

class Results(TypedDict):
    role: str | None
    sid: str | None
    latency: float | None
    upload: float | None
    download: float | None

#global variables
CLIENT: Client = {
    "control_channel": None,
    "throughput_channel": None,
    "latency_channel": None,
    "t0_latency": None,
    "t1_latency": None,
    "t0_throughput": None,
    "t1_throughput": None,
    "qtd_packages": 0
}
SERVER: Server = {
    "t0_latency": None,
    "t1_latency": None,
    "t0_throughput": None,
    "t1_throughput": None,
    "qtd_packages": 0
}

RESULTS: Results = {
    "role": None,
    "sid": None,
    "latency": None,
    "upload": None,
    "download": None
}

LATENCY = "latency"
THROUGHPUT = "throughput"
CONTROL = "control"
END_LATENCY = "Fim latência"
END_THROUGHPUT = 'fim'
SID = None
ROLE = None #OBS: a client is the peer who makes the offer while the server accepts the offer
#flags for logs
INFO = False
DEBUG = True


latency_finished = asyncio.Event()
snapshot_received = asyncio.Event()
channels: dict[str, RTCDataChannel] = {}
server_peers: list[Peer] = []

#connect to server
@sio.event
async def connect():
    await sio.emit("connected")


#disconnect from server
@sio.event
async def disconnect():
    if INFO:
        print("INFO - Desconectado do servidor")


@sio.on("new_peer")
async def new_peer_on_server(data):
    if INFO:
        print("INFO - novo par no servidor")
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

    #after receveing the snapshot from server the peer needs to remove itself from it's local list
    for peer in server_peers:
        if(peer["sid"] == data["sid"]):
            server_peers.remove(peer)
    if DEBUG:
        print(f'DEBUG - snapshot recebido e tratado: {server_peers}')
    await sio.emit("ready_to_start")


@sio.on("role_defined")
async def start_test(data):
    global ROLE
    ROLE = data["role"]
    RESULTS["role"] = ROLE

    if ROLE == 'client':
        update_peers_list(data["peer"], 'server')
        if DEBUG:
            print(f'DEBUG - lista local atualizada: {server_peers}')
        target = data["peer"]["target"]
        await client_make_offer(target_name=target)
    else:
        update_peers_list(data["peer"], 'client')
        if DEBUG:
            print(f'DEBUG - lista local atualizada: {server_peers}')


#this method receives the answer from the server peer.
@sio.on("answer")
async def client_receives_answer(data):
    if INFO:
        print("INFO - answer do par servidor recebida no cliente")
    sdp = RTCSessionDescription(sdp=data["answer"]["sdp"], type=data["answer"]["type"])
    await peer.setRemoteDescription(sdp) #this is the moment the connection is stablished


#region Client Make Offer
#client runs this method to make his offer to peer server
async def client_make_offer(target_name):

    #all channels must be created before the connection stablishment - these channels are created on client peer
    CLIENT["control_channel"] = peer.createDataChannel(CONTROL)
    CLIENT["throughput_channel"] = peer.createDataChannel(THROUGHPUT)
    CLIENT["latency_channel"] = peer.createDataChannel(LATENCY)

    #region Create SDP offer
    offer = await peer.createOffer()
    await peer.setLocalDescription(offer)

    if INFO:
        print('INFO - oferta criada no par cliente')
    #print(f'debug - sdp do peer: {peer.localDescription.sdp}')
    await sio.emit("offer", {
        "to": target_name,
        "offer": {
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp
        }
    })
    #endregion

    @CLIENT["control_channel"].on("open")
    async def control_task():
        CLIENT["control_channel"].send('O teste de LATÊNCIA irá começar...')

        @CLIENT["latency_channel"].on("open")
        def on_latency_channel():
            asyncio.create_task(client_send_lat_package(CLIENT["latency_channel"]))

        @CLIENT["throughput_channel"].on("open")
        async def on_throughput_channel():
            await latency_finished.wait() #wait for latency test to be finished
            CLIENT["control_channel"].send('O teste de VAZÃO irá começar...')
            asyncio.create_task(calculate_throughput(CLIENT["throughput_channel"]))

    @CLIENT["latency_channel"].on("message")
    def on_latency_message(message):
        CLIENT["t1_latency"] = time.time_ns()
        if INFO:
            print("INFO - <<< recebi LAT-ACK")
        client_send_ack(CLIENT["latency_channel"])
        calc_latency_a_b = (CLIENT["t1_latency"] - CLIENT["t0_latency"])/(10**6)

        if ROLE == 'client':
            RESULTS["latency"] = calc_latency_a_b

        if INFO:
            print(f'INFO - LATÊNCIA a=>b {calc_latency_a_b} ms')
        CLIENT["control_channel"].send(f'LATÊNCIA a=>b {calc_latency_a_b} ms')

    @CLIENT["control_channel"].on("message")
    async def on_control_message(message):
        msg = try_parse_json(message)
        if message == END_LATENCY:
            print(f'[CONTROLE]\t {message}')
            latency_finished.set()
        elif msg is not None:
            RESULTS["upload"] = msg["value"]
        else:
            print(f"[CONTROLE]\t {message}")

    @CLIENT["throughput_channel"].on("message")
    def on_throughput_message(message):
        if CLIENT["qtd_packages"] == 0:
            CLIENT["t0_throughput"] = time.time()  # retorna o tempo em segundos
            # print(f'debug - tamanho do pacote recebido {len(message)}') #sys.getsizeof(package) retorna o tamanho do objeto Python na memória
        CLIENT["qtd_packages"] = CLIENT["qtd_packages"] + 1
        if message == END_THROUGHPUT:
            CLIENT["t1_throughput"] = time.time()
            tempo = CLIENT["t1_throughput"] - CLIENT["t0_throughput"]
            vazao_em_bytes = ((CLIENT["qtd_packages"] - 1) * 1400) / tempo  # 1400 é o tamanho do pacote

            vazao_em_MB = round(vazao_em_bytes / 10**6, 2)
            vazao_em_Mbps = vazao_em_MB * 8

            if ROLE == 'client':
                if DEBUG:
                    print(f'sou cliente e ja tenho o download: {vazao_em_Mbps} Mbps')
                RESULTS["download"] = vazao_em_Mbps #It's here when the tests finish for client
                print(f'INFO - Resultados do cliente: {RESULTS}')
                save_to_file(RESULTS)

            CLIENT["control_channel"].send(
                f'RESULTADO DO TESTE DE UPLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
            CLIENT["control_channel"].send(json.dumps({
                "msg": "upload",
                "value": vazao_em_Mbps
            }))

            print(f'INFO - RESULTADO DO TESTE DE DOWNLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
#endregion

#region Server Receives Offer

@sio.on("offer")
async def server_receives_offer(data):

    if DEBUG:
        print('DEBUG - offer recebida no server_peer')

    #region Cria resposta SDP
    sdp = RTCSessionDescription(sdp=data["offer"]["sdp"], type=data["offer"]["type"])
    await peer.setRemoteDescription(sdp)

    answer = await peer.createAnswer()
    if DEBUG:
        print('DEBUG - answer criada no server_peer')
    await peer.setLocalDescription(answer)

    await sio.emit("answer", {
        "to": data["from"], #data["from"] é o sid do client_peer OK
        "answer": {
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp
        }
    })
    #endregion

    @peer.on("datachannel")
    def on_datachannel(received_channel):
        if (received_channel.label == CONTROL):
            channels[CONTROL] = received_channel
            @received_channel.on("message")
            async def on_control_message(message):
                msg = try_parse_json(message)
                if msg is not None:
                    RESULTS["upload"] = msg["value"] #It's here when the tests finish for server
                    print(f'INFO - Resultados do servidor: {RESULTS}')
                    save_to_file(RESULTS)

        if received_channel.label == LATENCY:
            channels[LATENCY] = received_channel
            @received_channel.on("message")
            def on_latency_message(message):
                if message == 'LAT':
                    if INFO:
                        print("INFO - <<< recebi LAT")
                    asyncio.create_task(server_send_lat_ack(received_channel))
                else:
                    SERVER["t1_latency"] = time.time_ns()
                    if INFO:
                        print("[LATÊNCIA]\t <<< recebi ACK")
                    calc_latency_b_a = (SERVER["t1_latency"] - SERVER["t0_latency"]) / (10 ** 6)
                    if INFO:
                        print(f'INFO - LATÊNCIA b=>a {calc_latency_b_a} ms')

                    if ROLE == 'server':
                        RESULTS[LATENCY] = calc_latency_b_a

                    channels[CONTROL].send(f'LATÊNCIA b=>a {calc_latency_b_a} ms')
                    channels[CONTROL].send(END_LATENCY)

        if received_channel.label == THROUGHPUT:
            channels[THROUGHPUT] = received_channel
            @received_channel.on("message")
            def on_throughput_message(message):
                if SERVER["qtd_packages"] == 0:
                    SERVER["t0_throughput"] = time.time() #retorna o tempo em segundos
                    #print(f'debug - tamanho do pacote recebido {len(message)}') #sys.getsizeof(package) retorna o tamanho do objeto Python na memória
                SERVER["qtd_packages"] = SERVER["qtd_packages"] + 1
                if message == END_THROUGHPUT:
                    SERVER["t1_throughput"] = time.time()
                    tempo = SERVER["t1_throughput"] - SERVER["t0_throughput"]
                    vazao_em_bytes = ((SERVER["qtd_packages"]-1) * 1400) / tempo #1400 é o tamanho do pacote
                    vazao_em_MB = round(vazao_em_bytes / 10**6, 2)
                    vazao_em_Mbps = vazao_em_MB * 8

                    if ROLE == 'server':
                        RESULTS["download"] = vazao_em_Mbps

                    channels[CONTROL].send(f'RESULTADO DO TESTE DE UPLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
                    channels[CONTROL].send(json.dumps({
                        "msg": "upload",
                        "value": vazao_em_Mbps
                    }))

                    print(f'INFO - RESULTADO DO TESTE DE DOWNLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
                    asyncio.create_task(calculate_throughput(channels[THROUGHPUT]))
#endregion

#region Calculate and send latency package
async def server_send_lat_ack(latency_channel):
    package = 'LAT-ACK'
    SERVER["t0_latency"] = time.time_ns()
    latency_channel.send(package)
    print("[LATÊNCIA]\t >>> enviei LAT-ACK")
#endregion

async def client_send_lat_package(latency_channel):
    package = 'LAT'
    CLIENT["t0_latency"] = time.time_ns()
    latency_channel.send(package)
    print("[LATÊNCIA]\t >>> enviei LAT")

def client_send_ack(latency_channel):
    package = 'ACK'
    latency_channel.send(package)
    print("[LATÊNCIA]\t >>> respondi ACK")

def update_peers_list(this, role):
    for peer in server_peers:
        if peer["sid"] == this["target"]:
            peer["role"] = role
            peer["status"] = 'OCCUPIED'
            peer["target"] = SID

def try_parse_json(message):
    try:
        return json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None

def save_to_file(results):
    results_data_frame = pd.DataFrame([results])
    results_data_frame = results_data_frame.rename(columns={
        "latency": "latency (ms)",
        "upload": "upload (Mbps)",
        "download": "download (Mbps)",
    })
    if DEBUG:
        print(f'DataFrame: {results_data_frame}')
    file_exists = os.path.exists('results.csv')
    results_data_frame.to_csv("results.csv", mode='a', header=not file_exists,index=False)


async def calculate_throughput(throughput_channel):
    try:
        package = bytes(1400)
        tam_total_dados = 10 * 10 ** 6  # enviarei no total 10MB
        qtd_pacotes = tam_total_dados // len(package)
        tam_pacote = len(package)
        if DEBUG:
            print(f'DEBUG - o envios dos pacotes vai começar agora. \nVou enviar {qtd_pacotes} pacotes de tamanho {tam_pacote}')
            print("DEBUG - ICE:", peer.iceConnectionState)
            print("DEBUG - DTLS:", peer.connectionState)
        for i in range(0, qtd_pacotes):
            throughput_channel.send(package)
        throughput_channel.send(END_THROUGHPUT)
    except Exception as e:
        print(f'Erro no througput: {e}')

# Função principal para iniciar o cliente e conectar
async def main():

    # Conectando ao servidor
    await sio.connect('http://localhost:5000')

    #print("Conectando... Aguarde o canal ser estabelecido.")

    try:
        await sio.wait()
    except KeyboardInterrupt:
        print("\nSaindo...")
        #control_task.cancel()
        await sio.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
