from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
import asyncio
import socketio
import sys
import time

#creat a Socket.IO client
sio = socketio.AsyncClient()
peer = RTCPeerConnection()


# Variáveis globais para o canal
#control_channel = None
#channel_vazao = None
#channel_ping = None

ping_t0 = None
ping_t1 = None
vazao_t0 = None
vazao_t1 = None
qtd = 0


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

        if (received_channel.label == "controle"):
            channels["controle"] = received_channel

            @received_channel.on("message")  # verificar se seria mensagem mesmo o evento
            def on_message(message):
                print(f"[CONTROLE]\t {message}")

        if received_channel.label == "ping":
            channels["ping"] = received_channel

            @received_channel.on("message")
            def on_ping(message):
                if message == 'PING':
                    print("[PING]\t <<< recebi PING")
                    asyncio.create_task(envia_ping(received_channel))
                else:
                    global ping_t0,ping_t1
                    ping_t1 = time.time_ns()
                    print("[PING]\t <<< recebi ACK")
                    calculo_ping_b_a = (ping_t1 - ping_t0) / (10 ** 6)
                    print(f'[  INFO  ]\t PING b=>a {calculo_ping_b_a} ms')
                    channels["controle"].send(f'PING b=>a {calculo_ping_b_a} ms')
                    channels["controle"].send("Fim ping")

        if received_channel.label == "vazao":
            #print('canal vazao recebido')
            channels["vazao"] = received_channel
            @received_channel.on("message")
            def on_message(message):
                global qtd, vazao_t0
                if qtd == 0:
                    vazao_t0 = time.time() #retorna o tempo em segundos
                    #print(f'debug - tamanho do pacote recebido {len(message)}') #sys.getsizeof(package) retorna o tamanho do objeto Python na memória
                qtd = qtd+1 #Está contando TODOS os pacotes que estou enviando, inclusive o fim. Preciso subtrair ele no calculo? Creio que sim, já que ele tem um tamanho diferente dos pacotes que gerei, e portanto o calculo não ficaria preciso
                if message == "fim":
                    global vazao_t1
                    vazao_t1 = time.time()
                    #print(f"debug - {message}. Recebi {qtd} pacotes")
                    #print(f'debug - vazao_t0 = {vazao_t0} e vazao_t1 = {vazao_t1}')
                    tempo = vazao_t1 - vazao_t0
                    print(f'debug - recebi {qtd-1} pacotes em {tempo}s')
                    vazao_em_bytes = ((qtd-1) * 1400) / tempo #1400 é o tamanho do pacote
                    vazao_em_MB = vazao_em_bytes / 10**6
                    vazao_em_Mb = (vazao_em_bytes * 8) / 10**6
                    print(f'[VAZÃO]\tPortanto a vazão de a=>b é {vazao_em_bytes} B/s ou {vazao_em_MB} MB/s ou {vazao_em_Mb * 8} Mbps') #confirmar se o cálculo está correto








#region Cálculo e envio do ping
async def envia_ping(channel_vazao):
    global ping_t0
    package = 'PING-ACK'
    ping_t0 = time.time_ns()
    channel_vazao.send(package)
    print("[PING]\t >>> enviei PING-ACK")

'''def responde_pong(channel_vazao):
    package = 'PONG'
    channel_vazao.send(package)
    print("[PING]\t >>> respondi PONG")'''
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