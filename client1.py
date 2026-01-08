from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
import asyncio
import socketio
import sys
import time

#creat a Socket.IO client
sio = socketio.AsyncClient()
peer = RTCPeerConnection()

# Variável global para o canal
control_channel = None #chamar de canal de controle
channel_vazao = None
channel_ping = None
ping_task = None

t0 = None
t1 = None
ping_finished = asyncio.Event()

#conect to server
@sio.event
async def connect():
    print("Conectado ao servidor")
    await sio.emit("join", {"name": "peer1"})
    await run_offer(target_name="peer2") #I guess I need to run the same code but with names exchanged


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


#this method receives the answer of the peer.
@sio.on("answer")
async def on_answer(data):
    print("debug - answer recebida no peer1")
    sdp = RTCSessionDescription(sdp=data["answer"]["sdp"], type=data["answer"]["type"])
    await peer.setRemoteDescription(sdp) #a partir daqui ele já nao usa mais o servidor rendezvous



async def run_offer(target_name):

    control_channel = peer.createDataChannel('controle')
    global channel_vazao, channel_ping
    channel_vazao = peer.createDataChannel("vazao") #aparentemente preciso criar todos os canais antes de estabelecer a conexão
    channel_ping = peer.createDataChannel("ping")

    #region Cria oferta SDP
    offer = await peer.createOffer()  # create the SDP offer - IS CORRECT TO SAY 'SDP OFFER'?
    await peer.setLocalDescription(
        offer)  # generate the SDP description of the offer - the local description is the offer

    # await offer_peer.setRemoteDescription(answer_peer.localDescription) #set its remote description as the answer peer's local description
    print('debug - oferta criada no peer 1')
    await sio.emit("offer", { #esse offer que ele está chamando é no client ou é no server?
        "to": target_name,
        "from": "peer1", #aparentemente o erro está aqui
        "offer": {
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp
        }
    })
    #endregion

    @control_channel.on("open")
    async def control_task():
        control_channel.send('O teste de PING irá começar...')

        @channel_ping.on("open")
        def on_channel_ping():
            asyncio.create_task(envia_ping(channel_ping))

        @channel_vazao.on("open")
        async def on_channel_vazao():
            await ping_finished.wait() #espera o teste de ping terminar
            control_channel.send('O teste de VAZÃO irá começar...')
            asyncio.create_task(calculate_throughput(channel_vazao, control_channel))
            #registrar um evento await pra esperar por um ACK do outro lado confirmando que o teste de vazão pode começar
            #confirmar com Everthon: acho que preciso garantir que nenhum canal esteja sendo usado, apenas o de vazão para dar um resultado mais fidedigno
            #await asyncio.sleep(2) #garante que todas as mensagens do canal de controle já tenham chegado.
            asyncio.create_task(calculate_throughput(channel_vazao,control_channel))


    @channel_ping.on("message")
    def on_message(message):
        global t0, t1
        t1 = time.time_ns()
        print("[PING]\t <<< recebi PING-ACK")
        responde_ack(channel_ping)
        calculo_ping_a_b = (t1 - t0)/(10**6)
        print(f'[  INFO  ]\t PING a=>b {calculo_ping_a_b} ms')
        control_channel.send(f'PING a=>b {calculo_ping_a_b} ms')


    @control_channel.on("message")
    def on_message(message):
        if message == "Fim ping":
            print(f'[CONTROLE]\t {message}')
            ping_finished.set()
        else:
            print(f"[CONTROLE]\t {message}")


async def envia_ping(channel_ping):
    global t0
    package = 'PING'
    t0 = time.time_ns()
    channel_ping.send(package)
    print("[PING]\t >>> enviei PING")

def responde_ack(channel_ping):
    package = 'ACK'
    channel_ping.send(package)
    print("[PING]\t >>> respondi ACK")


async def calculate_throughput(channel_vazao,control_channel):
    package = bytes(1400)
    tam_total_dados = 10 * 10 ** 6  # enviarei no total 10MB
    qtd_pacotes = tam_total_dados // len(package)
    tam_pacote = len(package)
    print(f'debug - o envios dos pacotes vai começar agora. \nVou enviar {qtd_pacotes} pacotes de tamanho {tam_pacote}')
    for i in range(0, qtd_pacotes):
        channel_vazao.send(package)
    channel_vazao.send('fim')

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
