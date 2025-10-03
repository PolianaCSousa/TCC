from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
import asyncio
import socketio
import threading
import sys
import time

#creat a Socket.IO client
sio = socketio.AsyncClient()
peer = RTCPeerConnection()

# Variável global para o canal
channel = None
chat_ready = False
message_queue = asyncio.Queue() #PRECISO ADICIONAR ISSO

#logs
def channel_log(channel, t, message):
    print(f'channel({channel}) {t} : {message}')

def send_message(channel, message):
    channel_log(channel, ">", message)
    channel.send(message)


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
    def on_datachannel(channel):
        print("canal recebido")

        @channel.on("message")
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


#my run_offer is the make_offer of the gpt
async def run_offer(target_name):
    global channel , chat_ready #ADICIONEI O CHAT_READY

    # I need to create the channel before the offer ?
    # create a data channel with the given label - returns a RTCDataChannel object. With RTCDataChannel I can send and receive data.
    channel = peer.createDataChannel('chat')

    channel_log(channel, "-", "channel created")

    # @channel.on() register an event and what happens when the event occurs (same as event listeners of JS)
    # O handler do "datachannel" precisa estar registrado antes da troca de SDP terminar, senão o canal pode chegar e você não terá como tratá-lo.
    @channel.on("open")
    def on_open():
        global chat_ready
        print('Canal aberto')
        channel.send('Olá do peer1!')
        chat_ready = True
        time.sleep(5)
        calculate_throughput()

    @channel.on("message")
    def on_message(message):
        print(f"{message}")


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



def calculate_throughput():
    vazao = peer.createDataChannel("vazao")




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
                send_message(channel, f"P1: {message}")

            # Marca a tarefa como concluída
            message_queue.task_done()
        except Exception as e:
            print(f"Erro ao processar mensagem: {e}")

# Variável global para o loop
loop = None

# Função principal para iniciar o cliente e conectar
async def main():
    global loop
    loop = asyncio.get_event_loop() #retorna o loop de eventos atual, que é o núcleo onde tudo do asyncio acontece (é o motor do asyncio)

    # Inicia task para processar mensagens
    processor_task = asyncio.create_task(message_processor()) #ao criar a task o async.io roda o metodo message_processor paralelamente

    # Inicia thread para input do usuário
    input_thread = threading.Thread(target=input_handler, daemon=True)
    input_thread.start()

    # Conectando ao servidor
    await sio.connect('http://localhost:5000')

    print("Conectando... Aguarde o canal ser estabelecido.")

    try:
        # Aguarde até que a conexão seja estabelecida
        await sio.wait()
    except KeyboardInterrupt:
        print("\nDesconectando...")
        processor_task.cancel()
        await sio.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
