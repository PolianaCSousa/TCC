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
    await peer.setRemoteDescription(sdp)



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
        control_channel.send('O teste de PING-PONG irá começar...')

        @channel_ping.on("open")
        def on_channel_ping():
            asyncio.create_task(envia_ping(channel_ping))  # PAREI AQUI
        # asyncio.create_task(calculate_troughput(channel_vazao)) #PAREI AQUI

    @peer.on("datachannel")
    def on_datachannel(received_channel):
        # global channel_vazao, control_channel

        # channels[received_channel.label] = received_channel

        if (received_channel.label == "controle"):
            @received_channel.on("message")  # verificar se seria mensagem mesmo o evento
            def on_message(message):
                print(f"{message}")

        if received_channel.label == "píng":
            print(f'[PING]\t RECEBIDO no cliente1 {received_channel.id}')

            print("[PING]\t <<< recebi PING")
            responde_pong(received_channel)


async def envia_ping(channel_ping):
    package = 'PING'
    channel_ping.send(package)
    print("[PING]\t >>> enviei PING")

def responde_pong(channel_ping):
    package = 'PONG'
    channel_ping.send(package)
    print("[PING]\t >>> respondi PONG")


#region Cálculo e envio da vazão
async def throughput_task(channel_vazao):
    print(f'Os testes irão começar: \n')
    throughput_result = await calculate_throughput(channel_vazao)
    print(f'\nO teste de vazão terminou\nA vazão calculada foi de: {throughput_result}mbps')
    return throughput_result


async def calculate_throughput(channel_vazao):
    @channel_vazao.on("open")
    async def on_open():
        package = bytes(1400)
        tam_total_dados = 10 * 10 ** 6  # enviarei no total 10MB
        qtd_pacotes = tam_total_dados // sys.getsizeof(package)

        for i in range(0, qtd_pacotes):
            channel_vazao.send(f"pacote[{i}]")
            # channel_vazao.send(package)

'''
#  o codigo do send_packages eu passei pro calculate_throughput
async def send_packages(channel_vazao):
    #byte = np.int8.tobytes(1, byteorder='little')
    #package = [byte] * 1400

    #package = os.urandom(1400)
    package = bytes(1400)
    tam_total_dados = 10 * 10 ** 6  # enviarei no total 10MB
    qtd_pacotes = tam_total_dados // sys.getsizeof(package)

    for i in range(0, qtd_pacotes):
        channel_vazao.send(f"pacote[{i}]")
'''
#endregion

# Função principal para iniciar o cliente e conectar
async def main():
    # Inicia task do canal de controle
    #control = asyncio.create_task(control_task())

    #onde coloco o await control?

    # Inicia task do canal de vazão - acho que não preciso fazer isso, basta chamá-la dentro da task de controle

    # Inicia task do canal de ping - acho que não preciso fazer isso, basta chamá-la dentro da task de controle

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
