import socketio
from aiohttp import web
from typing import TypedDict

'''
STATUS do peer:
FREE
OCCUPIED
'''

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
        'status': 'FREE',
        'sid': sid,
    })


@sio.on("connected")
async def client_connected(sid):
    print(f'debug - O cliente {sid} está pronto para receber o snapshot. Enviando...')
    await sio.emit('snapshot', {"snapshot": peers, "sid": sid}, to=sid)
    await sio.emit('new_peer', {'role': None, 'target': None, 'status': None, 'sid': sid, }, skip_sid=sid)


@sio.event
def disconnect(sid, reason):
    print(f'disconnect {sid}')


@sio.on("ready_to_start")
async def matchmaking(sid):
    print(f'Peer {sid} emitiu ready_to_start')
    for peer in peers:
        if peer["sid"] != sid and peer["status"] == 'FREE':
            list_peer = peer
            oferer_peer = find_peer(sid)
            if oferer_peer["sid"] < list_peer["sid"]:
                #the first parameter is the client peer
                set_role_and_target(oferer_peer,list_peer)
                await sio.emit("role_defined", {"peer": list_peer, "role": 'server'}, to=list_peer["sid"]) #emit for server peer first
                await sio.emit("role_defined", {"peer": oferer_peer, "role": 'client'}, to=oferer_peer["sid"])
            else:
                set_role_and_target(list_peer,oferer_peer)
                await sio.emit("role_defined", {"peer": oferer_peer, "role": 'server'}, to=oferer_peer["sid"])
                await sio.emit("role_defined", {"peer": list_peer, "role": 'client'}, to=list_peer["sid"])
    print(f'Lista do servidor: {peers}')


#foward the offer
@sio.event
async def offer(sid,data):
    sdp = data["offer"]
    target_sid = data["to"]
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

def find_peer(sid):
    for peer in peers:
        if peer["sid"] == sid:
            return peer
    return None

def set_role_and_target(client, server):
    client["role"] = 'client'
    client["target"] = server["sid"]
    client["status"] = 'OCCUPIED'
    server["role"] = 'server'
    server["target"] = client["sid"]
    server["status"] = 'OCCUPIED'

# Run the aiohttp server manually with a specific host and port
if __name__ == "__main__":
    web.run_app(app, host="localhost", port=5000)