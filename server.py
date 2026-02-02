import socketio
from aiohttp import web
from typing import TypedDict

#create a Socket.IO server
sio = socketio.AsyncServer()
app = web.Application() #create a server (application) with aiohttp

#A server configured for Aiohttp must be attached to an existing application:
sio.attach(app)

#typing for peers list structure
class Peer(TypedDict):
    role: str | None
    target: str | None
    status: str | None
    sid: str | None

peers: list[Peer] = [] #key: username value: sid (sessionID or socketID)


#the sid is passed automatically through the socket.io when a client connects to the server - PAREI AQUI
@sio.event
async def connect(sid, environ, auth):
    peers.append({
        'role': None,
        'target': None,
        'status': None,
        'sid': sid,
    })
    await sio.emit('snapshot', {"snapshot": peers, "sid": sid}, to=sid)
    await sio.emit('new_peer', {'role': None, 'target': None, 'status': None, 'sid': sid, }, skip_sid=sid)



@sio.event
def disconnect(sid, reason):
    print(f'disconnect {sid}')


#Acho que vou definir os nomes e pares aqui no metodo join, e depois de definidos vou informar aos pares quem eles são e com quem eles se conectam
@sio.event
async def join(sid):
    print(f'peer {sid} joined')
    #await sio.emit('new_peer', {'role': None, 'target': None, 'status': None, 'sid': sid, }, skip_sid=sid)
    #print(f'connect {sid}')


#foward the offer
@sio.event
async def offer(sid,data):
    sdp = data["offer"]
    target = data["to"] #em que momento o cliente definiu pra quem mandar? dentro do sio.emit("offer", ...)
    target_sid = peers.get(target) #eu achei que ele enviava o sid e não o nome
    print(f'debug - sdp do offer: \n{sdp}')
    if target_sid:
        await sio.emit("offer",{"from": sid,"offer": sdp}, to=target_sid)

#foward the answer
@sio.event
async def answer(sid,data):
    print('debug - processamento do answer do peer2')
    sdp = data["answer"]
    target_sid = data["to"]
    #target_sid = peers.get(target) - eu ja estou recebendo o sid do peer1
    print('target_sid: ', target_sid)
    print(f'debug - sdp do answer: \n{sdp}')
    if target_sid:
        await sio.emit("answer",{"from": sid, "answer": sdp}, to=target_sid)

#foward the ICE candidate to the peer
@sio.event
async def candidate(sid, data):
    target = data["to"]
    target_sid = peers.get(target)

    if target_sid:
        await sio.emit("candidate", {"from": sid, "candidate": data["candidate"]}, to=target_sid)


# Run the aiohttp server manually with a specific host and port
if __name__ == "__main__":
    web.run_app(app, host="localhost", port=5000)