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
    t0_throughput: int | None
    t1_throughput: int | None

class Server(TypedDict):
    t0_ping: int | None
    t1_ping: int | None
    t0_throughput: int | None
    t1_throughput: int | None
    qtd_packages: int

#global variables
CLIENT: Client = {
    "control_channel": None,
    "throughput_channel": None,
    "ping_channel": None,
    "t0_ping": None,
    "t1_ping": None,
    "t0_throughput": None,
    "t1_throughput": None
}
SERVER: Server = {
    "t0_ping": None,
    "t1_ping": None,
    "t0_throughput": None,
    "t1_throughput": None,
    "qtd_packages": 0
}


#control_channel = None
#throughput_channel = None
#ping_channel = None
ping_task = None
#t0_ping = None
#t1_ping = None
#t0_throughput = None
#t1_throughput = None

#flag to signalize if the peer is a client or a server
#OBS: a client is the peer who makes the offer while the server accepts the offer
ROLE = 'client'

ping_finished = asyncio.Event()

#conect to server
@sio.event
async def connect():
    print("Conectado ao servidor")
    await sio.emit("join", {"name": "peer1"})
    if ROLE == 'client':
        await client_make_offer(target_name="peer2")

@sio.event
async def disconnect():
    print("Desconectado do servidor")


#this method response to an offer from another peer - the offer that comes from the server
@sio.on("offer")
async def on_offer(data):
    sdp = RTCSessionDescription(sdp=data["offer"]["sdp"], type=data["offer"]["type"])
    await peer.setRemoteDescription(sdp)

    @peer.on("datachannel")
    def on_datachannel(channel_msg):
        print("canal recebido")

        @channel_msg.on("message")
        def on_message(message):
            print("💬", message)

    answer = await peer.createAnswer()
    await peer.setLocalDescription(answer)

    await sio.emit("answer", {
        "to": data["from"],
        "answer": {
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp
        }
    })


#this method receives the answer of the peer server.
@sio.on("answer")
async def client_receive_answer(data):
    print("debug - answer recebida no peer1 (cliente)")
    sdp = RTCSessionDescription(sdp=data["answer"]["sdp"], type=data["answer"]["type"])
    await peer.setRemoteDescription(sdp) #this is the moment the connection is stablished


#region Client Make Offer
#client runs this method to make his offer to peer server
async def client_make_offer(target_name):

    #all channels must be created before the connection stablishment - these channels are created on client peer
    CLIENT["control_channel"] = peer.createDataChannel('controle')
    CLIENT["throughput_channel"] = peer.createDataChannel("vazao")
    CLIENT["ping_channel"] = peer.createDataChannel("ping")

    #region Cria oferta SDP
    offer = await peer.createOffer()  # create the SDP offer - IS CORRECT TO SAY 'SDP OFFER'?
    await peer.setLocalDescription(
        offer)  # generate the SDP description of the offer - the local description is the offer

    # await offer_peer.setRemoteDescription(answer_peer.localDescription) #set its remote description as the answer peer's local description
    print('debug - oferta criada no peer 1')
    #print(f'debug - sdp do peer: {peer.localDescription.sdp}')
    await sio.emit("offer", { #esse offer que ele está chamando é no client ou é no server?
        "to": target_name,
        "from": "peer1", #aparentemente o erro está aqui
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
            await ping_finished.wait() #espera o teste de ping terminar
            CLIENT["control_channel"].send('O teste de VAZÃO irá começar...')
            asyncio.create_task(client_calculate_throughput(CLIENT["throughput_channel"], CLIENT["control_channel"]))


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

async def client_calculate_throughput(throughput_channel,control_channel):
    package = bytes(1400)
    tam_total_dados = 10 * 10 ** 6  # enviarei no total 10MB
    qtd_pacotes = tam_total_dados // len(package)
    tam_pacote = len(package)
    print(f'debug - o envios dos pacotes vai começar agora. \nVou enviar {qtd_pacotes} pacotes de tamanho {tam_pacote}')
    for i in range(0, qtd_pacotes):
        throughput_channel.send(package)
    throughput_channel.send('fim')

#region Server Receives Offer

#endregion

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
