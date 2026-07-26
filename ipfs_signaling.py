import asyncio
import json
from constants import(
    FREE,
    SERVER,
    CLIENT,
    PAIRING,
    PAIRED
)

HEARTBEAT_INTERVAL = 2  # segundos entre cada announce


class IpfsSignaling:

    def __init__(self, client, topic):
        self.kubo_client = client          # instancia de KuboClient
        self.topic = topic
        self.my_id = None             # meu PeerID (legivel), preenchido no start()
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
        self._tasks.append(asyncio.create_task(self._consume()))
        self._tasks.append(asyncio.create_task(self._heartbeat()))

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
        # Enquanto livre, anuncia presenca periodicamente pros outros me acharem.
        while self.status == FREE:
            await self._publish({"kind": "announce", "status": FREE})
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
        elif kind in self._handlers and data.get("to") == self.my_id:
            await self._handlers[kind](data)

    # --- Matchmaking ---------------------------------------------------------

    async def _on_announce(self, data):
        if self.status != FREE:
            return
        other = data["from"]
        # Regra deterministica: so o menor PeerID inicia o pareamento.
        if self.my_id < other:
            self.status = PAIRING
            self.partner = other
            await self.send("pair_request", other, {})

    async def _on_pair_request(self, data):
        if data.get("to") != self.my_id or self.status != FREE:
            return
        # Sou o alvo (PeerID maior) -> viro 'server' e aceito.
        self.partner = data["from"]
        self.role = SERVER
        self.status = PAIRED
        await self.send("pair_accept", self.partner, {})
        await self._finish_pairing()

    async def _on_pair_accept(self, data):
        if data.get("to") != self.my_id or self.status != PAIRING:
            return
        if data["from"] != self.partner:
            return
        # Meu pedido foi aceito -> sou o 'client' (offerer).
        self.role = CLIENT
        self.status = PAIRED
        await self._finish_pairing()

    async def _finish_pairing(self):
        self.paired.set()
        if "role_defined" in self._handlers and self.role == CLIENT:
            await self._handlers["role_defined"](self.partner)

    # --- Helper --------------------------------------------------------------

    async def _publish(self, payload):
        payload["from"] = self.my_id
        await self.kubo_client.pubsub_pub(self.topic, json.dumps(payload))
