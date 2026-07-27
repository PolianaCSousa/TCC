import asyncio
import json
import time
from constants import(
    FREE,
    SERVER,
    CLIENT,
    PAIRING,
    PAIRED
)
from isp import(
    get_asn
)

HEARTBEAT_INTERVAL = 2  # segundos entre cada announce
PEER_TIMEOUT = HEARTBEAT_INTERVAL * 3 # se tiver passado 6 segundos e o par nao tiver feito anúncio, considero que ele saiu
PAIR_TIMEOUT = 5 # segundos esperando o pair_accept antes de desistir


class IpfsSignaling:

    def __init__(self, client, topic):
        self.kubo_client = client          # instancia de KuboClient
        self.topic = topic
        self.my_id = None             # meu PeerID (legivel), preenchido no start()
        self.my_asn = None          # busca o ASN do ISP do peer
        self.peers = {}             # peer_id -> {"status", "asn", "last_seen"}
        self.status = FREE          # free -> pairing -> paired
        self.partner = None           # PeerID do parceiro
        self.role = None              # 'client' ou 'server'
        self._handlers = {}           # kind -> async handler(data)
        self.paired = asyncio.Event() # sinaliza quando o par foi definido
        self._tasks = []

    # --- API publica ---------------------------------------------------------

    def on(self, kind, handler):
        """Registra um handler async pra um tipo de mensagem (ex.: 'offer')."""
        self._handlers[kind] = handler


    async def start(self):
        self.my_id = await self.kubo_client.id()
        self.my_asn = get_asn()
        self._tasks.append(asyncio.create_task(self._consume()))
        self._tasks.append(asyncio.create_task(self._heartbeat()))
        self._tasks.append(asyncio.create_task(self._cleaner()))


    async def send(self, kind, to, extra):
        """Envia uma mensagem enderecada (ex.: offer/answer) ao parceiro."""
        payload = {"kind": kind, "to": to}
        payload.update(extra)
        await self._publish(payload)


    async def close(self):
        for task in self._tasks:
            task.cancel()

    # --- Loops internos ------------------------------------------------------

    async def _heartbeat(self):
        # anuncia SEMPRE, com o status atual (não para ao sair de FREE)
        while True:
            await self._publish({"kind": "announce", "status": self.status, "asn": self.my_asn})
            await asyncio.sleep(HEARTBEAT_INTERVAL)


    async def _consume(self):
        async for message in self.kubo_client.pubsub_sub(self.topic):
            data = json.loads(message["data"])
            if data.get("from") == self.my_id:
                continue  # ignora o proprio eco
            await self._dispatch(data)


    async def _dispatch(self, data):
        kind = data.get("kind")
        if kind == "announce":
            await self._on_announce(data)
        elif kind == "pair_request":
            await self._on_pair_request(data)
        elif kind == "pair_accept":
            await self._on_pair_accept(data)
        elif kind == "pair_reject":
            await self._on_pair_reject(data)
        elif kind in self._handlers and data.get("to") == self.my_id:
            await self._handlers[kind](data)


    async def _cleaner(self): #remove da lista local pares que se desconectaram
        while True:
            await asyncio.sleep(PEER_TIMEOUT)
            now = time.monotonic()
            disconnected = []

            for id, p in self.peers.items():
                if now - p["last_seen"] > PEER_TIMEOUT:
                    disconnected.append(id)

            for id in disconnected:
                del self.peers[id]


    # --- Matchmaking ---------------------------------------------------------

    async def _on_announce(self, data):
        other_peer_id = data["from"]
        self.peers[other_peer_id] = {
            "status": data.get("status"),
            "asn": data.get("asn"),
            "last_seen": time.monotonic(),
        }
        if self.status != FREE:
            return
        partner = self._select_partner()
        if partner is None:
            return
        if self.my_id < partner:
            self.status = PAIRING
            self.partner = partner
            await self.send("pair_request", partner, {})
            asyncio.create_task(self._pair_timeout(partner))


    def _select_partner(self):
        other_asn = []
        same_asn = []
        for peer_id, p in self.peers.items():
            if p["status"] != FREE or p["asn"] is None:
                continue
            if p["asn"] != self.my_asn:
                other_asn.append(peer_id)
            else:
                same_asn.append(peer_id)
        
        candidates = other_asn or same_asn
        if not candidates:
            return None
        return min(candidates) # retorna o candidato de menor peerID


    async def _on_pair_request(self, data):
        if data.get("to") != self.my_id:
            return
        requester = data["from"]
        if self.status != FREE:
            await self.send("pair_reject", requester, {})
            return
        self.partner = requester
        self.role = SERVER
        self.status = PAIRED
        await self.send("pair_accept", self.partner, {})
        await self._finish_pairing()


    async def _on_pair_accept(self, data):
        if data.get("to") != self.my_id or self.status != PAIRING:
            return
        if data["from"] != self.partner:
            return
        self.role = CLIENT
        self.status = PAIRED
        await self._finish_pairing()

    async def _on_pair_reject(self, data):
        if data.get("to") != self.my_id or self.status != PAIRING:
            return
        if data["from"] != self.partner:
            return
        self._reset_to_free()   # recusado → volto a livre; o próximo announce me faz reescolher

    def _reset_to_free(self):
        self.status = FREE
        self.partner = None
        self.role = None

    async def _pair_timeout(self, expected_partner):
        await asyncio.sleep(PAIR_TIMEOUT)
        # se ainda espero o MESMO alvo, desisto e volto a livre
        if self.status == PAIRING and self.partner == expected_partner:
            self._reset_to_free()


    async def _finish_pairing(self):
        self.paired.set()
        if "role_defined" in self._handlers and self.role == CLIENT:
            await self._handlers["role_defined"](self.partner)

    # --- Helper --------------------------------------------------------------

    async def _publish(self, payload):
        payload["from"] = self.my_id
        await self.kubo_client.pubsub_pub(self.topic, json.dumps(payload))
