from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
import asyncio
import socketio
import sys
import time

#creat a Socket.IO client
sio = socketio.AsyncClient()
peer = RTCPeerConnection()


# Variáveis globais para o canal
control_channel = None
channel_vazao = None
channel_ping = None


#conect to server
@sio.event
async def connect():
    print("Conectado ao servidor")
    await sio.emit("join", {"name": "peer2"})


@sio.event
async def disconnect():
    print("Desconectado do servidor")


channels = {}
teste_ping = {}
#this method response to an offer from another peer - the offer that comes from the server
@sio.on("offer")
async def on_offer(data):
    #global channel_vazao, control_channel
    print('debug - offer recebida no peer2')

    #region Cria resposta SDP
    sdp = RTCSessionDescription(sdp=data["offer"]["sdp"], type=data["offer"]["type"])
    await peer.setRemoteDescription(sdp)

    answer = await peer.createAnswer()
    print('debug - answer criada no peer2')
    await peer.setLocalDescription(answer)

    await sio.emit("answer", {
        "to": data["from"], #data["from"] é o sid do peer1 OK
        "answer": {
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp
        }
    })
    #endregion

    @peer.on("datachannel")
    def on_datachannel(received_channel):
        # global channel_vazao, control_channel

        # channels[received_channel.label] = received_channel

        if (received_channel.label == "controle"):
            @received_channel.on("message")  # verificar se seria mensagem mesmo o evento
            def on_message(message):
                print(f"[CONTROLE]\t {message}")

        if received_channel.label == "ping":
            @received_channel.on("message")
            def on_ping(message):
                if message == 'PING':
                    print("[PING]\t <<< recebi PING")
                    asyncio.create_task(envia_ping(received_channel))
                else:
                    print("[PING]\t <<< recebi PONG")
                    # responde_pong(received_channel)
                    # asyncio.create_task(envia_ping(received_channel))

                    #TO DO: depois que finalizar o teste eu posso enviar no canal de controle dizendo que finalizou.
                    #Para isso, vou ter que armazenar os canais em um dicionário para quando eu quiser mandar por eles,
                    #mesmo que eu não tenha recebido nada dele naquele momento.




#region Cálculo e envio do ping
async def envia_ping(channel_vazao):
    package = 'PING'
    channel_vazao.send(package)
    print("[PING]\t >>> enviei PING")

def responde_pong(channel_vazao):
    package = 'PONG'
    channel_vazao.send(package)
    print("[PING]\t >>> respondi PONG")
#endregion


#this method receives the answer of the peer.
@sio.on("answer")
async def on_answer(data):
    print("Resposta recebida")
    sdp = RTCSessionDescription(sdp=data["answer"]["sdp"], type=data["answer"]["type"])
    await peer.setRemoteDescription(sdp)


#my run_offer is the make_offer of the gpt
async def run_offer(target_name):
    # I need to create the channel before the offer ?
    # create a data channel with the given label - returns a RTCDataChannel object. With RTCDataChannel I can send and receive data.
    channel_msg = peer.createDataChannel('chat')
    channel_msg_log(channel_msg, "-", "channel_msg created")

    # @channel.on() register an event and what happens when the event occurs (same as event listeners of JS)
    # O handler do "datachannel" precisa estar registrado antes da troca de SDP terminar, senão o canal pode chegar e você não terá como tratá-lo.
    @channel_msg.on("open")
    def on_open():
        print('Canal aberto')
        channel_msg.send('Olá do peer!')

    @channel_msg.on("message")
    def on_message(message):
        print('Recebido', message)


    offer = await peer.createOffer()  # create the SDP offer - IS CORRECT TO SAY 'SDP OFFER'?
    await peer.setLocalDescription(
        offer)  # generate the SDP description of the offer - the local description is the offer




    # await offer_peer.setRemoteDescription(answer_peer.localDescription) #set its remote description as the answer peer's local description

    await sio.emit("offer", { #esse offer que ele está chamando é no client ou é no server?
        "to": "peer1",
        "offer": {
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp
        }
    })


# Função principal para iniciar o cliente e conectar
async def main():
    # Conectando ao servidor
    await sio.connect('http://localhost:5000')

    print("Conectando... Aguardando oferta do peer1...")

    try:
        await sio.wait()
    except KeyboardInterrupt:
        print("\nSaindo...")
        #control_task.cancel()
        await sio.disconnect()


if __name__ == "__main__":
    asyncio.run(main())