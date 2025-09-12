from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription, RTCDataChannel
import asyncio

'''
Fluxo do programa:

 - createOffer() / createAnswer() → criam as descrições SDP.

 - setLocalDescription() → define o que você está oferecendo/aceitando.

 - setRemoteDescription() → define o que o outro está oferecendo/aceitando.


Somente após esses passos, o evento "open" será disparado no canal, e o canal estará pronto para enviar/receber mensagens.

'''
def channel_log(channel, t, message):
    print(f'channel({channel}) {t} : {message}')

def send_message(channel, message):
    channel_log(channel, ">", message)
    channel.send(message)

#method of peer that creates an offer
async def run_offer(offer_peer):

    #I need to create the channel before the offer ?
    # create a data channel with the given label - returns a RTCDataChannel object. With RTCDataChannel I can send and receive data.
    channel = offer_peer.createDataChannel('chat')
    channel_log(channel, "-", "channel created")

    # @channel.on() register an event and what happens when the event occurs (same as event listeners of JS)
    # O handler do "datachannel" precisa estar registrado antes da troca de SDP terminar, senão o canal pode chegar e você não terá como tratá-lo.
    @channel.on("open")
    def on_open():
        print('Canal aberto')
        channel.send('Olá do peer!')

    @channel.on("message")
    def on_message(message):
        print('Recebido', message)

    offer = await offer_peer.createOffer()  # create the SDP offer
    await offer_peer.setLocalDescription(offer)  # generate the SDP description of the offer - the local description is the offer

    #await offer_peer.setRemoteDescription(answer_peer.localDescription) #set its remote description as the answer peer's local description

    return offer_peer.localDescription


#method of the peer that will receive and accept the offer
async def run_answer(answer_peer, offer_peer_description):

    print('debug: on answer')

    await answer_peer.setRemoteDescription(offer_peer_description)  # receives the offer peer description and set its remote description as the offer description
    answer = await answer_peer.createAnswer() #creates the SDP answer
    await answer_peer.setLocalDescription(answer) #generate the SDP description of the answer - the local description is the answer

    #Verify if these handlelers must stay here or before the creation of the answer
    #the datachannel event is fired when the channel created in the offer peeer is received
    @answer_peer.on("datachannel")
    def on_datachannel(channel):
        channel_log(channel,"-","channel from remote peer received")

        @channel.on("message")
        def on_message(message):
            channel_log(channel,"<",message)

    return answer_peer.localDescription

async def main():
    # The RTCPeerConnection interface represents a WebRTC connection between the local computer and a remote peer. Each peer is a RTCPeerConnection instance
    offer_peer = RTCPeerConnection() #creates the peer
    answer_peer = RTCPeerConnection()

    #STEPS:
    #when creating an offer I need to set its description on the answer peer, so the run_offer method returns this description.
    #Before creating the answer I need to set the description of the offer peer on the remoteDescription of the answer peer.
    #After that, I can create the answer and I need return its description to the offer peer. I set the offer_peer remote right after the creation of answer_peer
    #OBS: the description is the SDP
    offer_peer_description = await run_offer(offer_peer)
    answer_peer_description = await run_answer(answer_peer, offer_peer_description)

    await offer_peer.setRemoteDescription(answer_peer_description)


if __name__ == '__main__':
    print('Iniciando...')
    asyncio.run(main())
    #asyncio.sleep(3)
    print('Encerrado')



