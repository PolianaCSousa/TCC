from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
import asyncio
import socketio
import threading

# creat a Socket.IO client
sio = socketio.AsyncClient()
peer = RTCPeerConnection()


# logs
def channel_log(channel, t, message):
    print(f'channel({channel}) {t} : {message}')


def send_message(channel, message):
    channel_log(channel, ">", message)
    channel.send(message)


# Variáveis globais para o canal e queue para mensagens
channel = None
chat_ready = False
message_queue = asyncio.Queue()


# conect to server
@sio.event
async def connect():
    print("Conectado ao servidor")
    await sio.emit("join", {"name": "peer2"})


@sio.event
async def disconnect():
    print("Desconectado do servidor")


# this method response to an offer from another peer - the offer that comes from the server
@sio.on("offer")
async def on_offer(data):
    global channel, chat_ready

    print('debug - offer recebida no peer2')
    sdp = RTCSessionDescription(sdp=data["offer"]["sdp"], type=data["offer"]["type"])
    await peer.setRemoteDescription(sdp)

    @peer.on("datachannel")
    def on_datachannel(received_channel):
        global channel, chat_ready
        channel = received_channel
        print("Canal recebido")

        @channel.on("open")
        def on_open():
            global chat_ready
            print("Canal aberto no peer2")
            chat_ready = True
            print("Chat pronto! Digite suas mensagens (ou 'quit' para sair):")

        @channel.on("message")
        def on_message(message):
            print(f"Peer1: {message}")

    answer = await peer.createAnswer()
    print('debug - answer criada no peer2')
    await peer.setLocalDescription(answer)

    await sio.emit("answer", {
        "to": data["from"],
        "answer": {
            "type": peer.localDescription.type,
            "sdp": peer.localDescription.sdp
        }
    })


# this method receives the answer of the peer.
@sio.on("answer")
async def on_answer(data):
    print("Resposta recebida")
    sdp = RTCSessionDescription(sdp=data["answer"]["sdp"], type=data["answer"]["type"])
    await peer.setRemoteDescription(sdp)


# after crating an offer the ICE candidate is generated and it needs to be sent to the other peer
@peer.on("icecandidate")
async def on_ice_candidate(candidate):
    if candidate:
        print("Novo ICE Candidate:", candidate)
        await sio.emit("candidate", {
            "to": "peer1",
            "candidate": {
                "candidate": candidate.candidate,
                "sdpMid": candidate.sdpMid,
                "sdpMLineIndex": candidate.sdpMLineIndex,
            }
        })


# receives the ICE candidate from the remote peer and set
@sio.on("candidate")
async def on_candidate(data):
    candidate = data["candidate"]
    await peer.addIceCandidate(
        RTCIceCandidate(
            sdpMid=candidate["sdpMid"],
            sdpMLineIndex=candidate["sdpMLineIndex"],
            candidate=candidate["candidate"]
        )
    )


# Função para lidar com input do usuário em thread separada
def input_handler():
    global message_queue
    while True:
        try:
            message = input()
            if message.lower() == 'quit':
                print("Saindo do chat...")
                break
            # Coloca a mensagem na queue de forma thread-safe
            asyncio.run_coroutine_threadsafe(message_queue.put(message), loop)
        except (EOFError, KeyboardInterrupt):
            print("\nSaindo do chat...")
            break


# Task para processar mensagens da queue
async def message_processor():
    global channel, chat_ready, message_queue
    while True:
        try:
            # Aguarda uma mensagem da queue
            message = await message_queue.get()

            # Verifica se o canal está pronto
            if channel and chat_ready:
                send_message(channel, f"Peer2: {message}")

            # Marca a tarefa como concluída
            message_queue.task_done()
        except Exception as e:
            print(f"Erro ao processar mensagem: {e}")


# Variável global para o loop
loop = None


# Função principal para iniciar o cliente e conectar
async def main():
    global loop
    loop = asyncio.get_event_loop()

    # Inicia task para processar mensagens
    processor_task = asyncio.create_task(message_processor())

    # Inicia thread para input do usuário
    input_thread = threading.Thread(target=input_handler, daemon=True)
    input_thread.start()

    # Conectando ao servidor
    await sio.connect('http://localhost:5000')

    print("Conectando... Aguardando oferta do peer1...")

    try:
        # Aguarde até que a conexão seja estabelecida
        await sio.wait()
    except KeyboardInterrupt:
        print("\nDesconectando...")
        processor_task.cancel()
        await sio.disconnect()


if __name__ == "__main__":
    asyncio.run(main())