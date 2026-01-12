from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCDataChannel
import asyncio
import socketio
import time
from typing import TypedDict

#creat a Socket.IO client and a peer for WebRTC connection
sio = socketio.AsyncClient()
peer = RTCPeerConnection()

#typing for global CLIENT and SERVER dictionaries
class Client(TypedDict):
    control_channel: RTCDataChannel | None
    throughput_channel: RTCDataChannel | None
    ping_channel: RTCDataChannel | None
    t0_ping: int | None
    t1_ping: int | None
    t0_throughput: int | float | None
    t1_throughput: int | float | None
    qtd_packages: int

class Server(TypedDict):
    t0_ping: int | None
    t1_ping: int | None
    t0_throughput: int | float | None
    t1_throughput: int | float | None
    qtd_packages: int

#global variables
CLIENT: Client = {
    "control_channel": None,
    "throughput_channel": None,
    "ping_channel": None,
    "t0_ping": None,
    "t1_ping": None,
    "t0_throughput": None,
    "t1_throughput": None,
    "qtd_packages": 0
}
SERVER: Server = {
    "t0_ping": None,
    "t1_ping": None,
    "t0_throughput": None,
    "t1_throughput": None,
    "qtd_packages": 0
}
#flag to signalize if the peer is a client or a server
#OBS: a client is the peer who makes the offer while the server accepts the offer
ROLE = 'client'
ping_finished = asyncio.Event()
channels: dict[str, RTCDataChannel] = {}

#connect to server
@sio.event
async def connect():
    print("Conectado ao servidor")
    if ROLE == 'client':
        await sio.emit("join", {"name": "client_peer"})
        await client_make_offer(target_name="server_peer")
    else:
        await sio.emit("join", {"name": "server_peer"})

#disconnect from server
@sio.event
async def disconnect():
    print("Desconectado do servidor")


#this method receives the answer from the server peer.
@sio.on("answer")
async def client_receives_answer(data):
    print("debug - answer recebida no client_peer (cliente)")
    sdp = RTCSessionDescription(sdp=data["answer"]["sdp"], type=data["answer"]["type"])
    await peer.setRemoteDescription(sdp) #this is the moment the connection is stablished


#region Client Make Offer
#client runs this method to make his offer to peer server
async def client_make_offer(target_name):

    #all channels must be created before the connection stablishment - these channels are created on client peer
    CLIENT["control_channel"] = peer.createDataChannel('controle')
    CLIENT["throughput_channel"] = peer.createDataChannel("vazao")
    CLIENT["ping_channel"] = peer.createDataChannel("ping")

    #region Create SDP offer
    offer = await peer.createOffer()
    await peer.setLocalDescription(offer)

    print('debug - oferta criada no client_peer')
    #print(f'debug - sdp do peer: {peer.localDescription.sdp}')
    await sio.emit("offer", {
        "to": target_name,
        "from": "client_peer",
        "offer": {
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp
        }
    })
    #endregion

    @CLIENT["control_channel"].on("open")
    async def control_task():
        CLIENT["control_channel"].send('O teste de PING irá começar...')

        @CLIENT["ping_channel"].on("open")
        def on_ping_channel():
            asyncio.create_task(client_send_ping(CLIENT["ping_channel"]))

        @CLIENT["throughput_channel"].on("open")
        async def on_throughput_channel():
            await ping_finished.wait() #wait for ping test to be finished
            CLIENT["control_channel"].send('O teste de VAZÃO irá começar...')
            asyncio.create_task(calculate_throughput(CLIENT["throughput_channel"]))


    @CLIENT["ping_channel"].on("message")
    def on_message(message):
        CLIENT["t1_ping"] = time.time_ns()
        print("[PING]\t <<< recebi PING-ACK")
        client_send_ack(CLIENT["ping_channel"])
        calculo_ping_a_b = (CLIENT["t1_ping"] - CLIENT["t0_ping"])/(10**6)
        print(f'[  INFO  ]\t PING a=>b {calculo_ping_a_b} ms')
        CLIENT["control_channel"].send(f'PING a=>b {calculo_ping_a_b} ms')


    @CLIENT["control_channel"].on("message")
    def on_message(message):
        if message == "Fim ping":
            print(f'[CONTROLE]\t {message}')
            ping_finished.set()
        else:
            print(f"[CONTROLE]\t {message}")

    @CLIENT["throughput_channel"].on("message")
    def on_message(message):
        if CLIENT["qtd_packages"] == 0:
            CLIENT["t0_throughput"] = time.time()  # retorna o tempo em segundos
            # print(f'debug - tamanho do pacote recebido {len(message)}') #sys.getsizeof(package) retorna o tamanho do objeto Python na memória
        CLIENT["qtd_packages"] = CLIENT["qtd_packages"] + 1
        if message == "fim":
            CLIENT["t1_throughput"] = time.time()
            tempo = CLIENT["t1_throughput"] - CLIENT["t0_throughput"]
            # print(f'debug - recebi {qtd_packages-1} pacotes em {tempo}s')
            vazao_em_bytes = ((CLIENT["qtd_packages"] - 1) * 1400) / tempo  # 1400 é o tamanho do pacote
            vazao_em_MB = vazao_em_bytes / 10 ** 6
            # vazao_em_Mb = (vazao_em_bytes * 8) / 10**6
            vazao_em_Mbps = vazao_em_MB * 8
            CLIENT["control_channel"].send(
                f'[INFO] RESULTADO DO TESTE DE UPLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
            print(f'[CONTROLE] RESULTADO DO TESTE DE DOWNLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
#endregion

#region Server Receives Offer

@sio.on("offer")
async def server_receives_offer(data):

    print('debug - offer recebida no server_peer')

    #region Cria resposta SDP
    sdp = RTCSessionDescription(sdp=data["offer"]["sdp"], type=data["offer"]["type"])
    await peer.setRemoteDescription(sdp)

    answer = await peer.createAnswer()
    print('debug - answer criada no server_peer')
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

        if (received_channel.label == "controle"):
            channels["controle"] = received_channel

            @received_channel.on("message")
            def on_message(message):
                print(f"[CONTROLE]\t {message}")

        if received_channel.label == "ping":
            channels["ping"] = received_channel

            @received_channel.on("message")
            def on_ping(message):
                if message == 'PING':
                    print("[PING]\t <<< recebi PING")
                    asyncio.create_task(server_send_ping_ack(received_channel))
                else:
                    SERVER["t1_ping"] = time.time_ns()
                    print("[PING]\t <<< recebi ACK")
                    calculo_ping_b_a = (SERVER["t1_ping"] - SERVER["t0_ping"]) / (10 ** 6)
                    print(f'[  INFO  ]\t PING b=>a {calculo_ping_b_a} ms')
                    channels["controle"].send(f'PING b=>a {calculo_ping_b_a} ms')
                    channels["controle"].send("Fim ping")

        if received_channel.label == "vazao":
            channels["vazao"] = received_channel
            @received_channel.on("message")
            def on_message(message):
                if SERVER["qtd_packages"] == 0:
                    SERVER["t0_throughput"] = time.time() #retorna o tempo em segundos
                    #print(f'debug - tamanho do pacote recebido {len(message)}') #sys.getsizeof(package) retorna o tamanho do objeto Python na memória
                SERVER["qtd_packages"] = SERVER["qtd_packages"] + 1
                if message == "fim":
                    SERVER["t1_throughput"] = time.time()
                    tempo = SERVER["t1_throughput"] - SERVER["t0_throughput"]
                    #print(f'debug - recebi {qtd_packages-1} pacotes em {tempo}s')
                    vazao_em_bytes = ((SERVER["qtd_packages"]-1) * 1400) / tempo #1400 é o tamanho do pacote
                    vazao_em_MB = vazao_em_bytes / 10**6
                    #vazao_em_Mb = (vazao_em_bytes * 8) / 10**6
                    vazao_em_Mbps = vazao_em_MB * 8
                    channels["controle"].send(f'[INFO] RESULTADO DO TESTE DE UPLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
                    print(f'[CONTROLE] RESULTADO DO TESTE DE DOWNLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
                    asyncio.create_task(calculate_throughput(channels["vazao"]))
#endregion

#region Cálculo e envio do ping
async def server_send_ping_ack(ping_channel):
    package = 'PING-ACK'
    SERVER["t0_ping"] = time.time_ns()
    ping_channel.send(package)
    print("[PING]\t >>> enviei PING-ACK")
#endregion

async def client_send_ping(ping_channel):
    package = 'PING'
    CLIENT["t0_ping"] = time.time_ns()
    ping_channel.send(package)
    print("[PING]\t >>> enviei PING")

def client_send_ack(ping_channel):
    package = 'ACK'
    ping_channel.send(package)
    print("[PING]\t >>> respondi ACK")

async def calculate_throughput(throughput_channel):
    package = bytes(1400)
    tam_total_dados = 10 * 10 ** 6  # enviarei no total 10MB
    qtd_pacotes = tam_total_dados // len(package)
    tam_pacote = len(package)
    print(f'debug - o envios dos pacotes vai começar agora. \nVou enviar {qtd_pacotes} pacotes de tamanho {tam_pacote}')
    for i in range(0, qtd_pacotes):
        throughput_channel.send(package)
    throughput_channel.send('fim')

# Função principal para iniciar o cliente e conectar
async def main():

    # Conectando ao servidor
    await sio.connect('http://localhost:5000')

    print("Conectando... Aguarde o canal ser estabelecido.")

    try:
        await sio.wait()
    except KeyboardInterrupt:
        print("\nSaindo...")
        #control_task.cancel()
        await sio.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
