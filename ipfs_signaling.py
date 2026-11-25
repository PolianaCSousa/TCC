import asyncio
import json
import logging
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

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 2  # seconds between announces
PEER_TIMEOUT = HEARTBEAT_INTERVAL * 3 # with no announce within this interval, the peer is considered gone
PAIR_TIMEOUT = 5 # seconds waiting for pair_accept before giving up


class IpfsSignaling:

    def __init__(self, client, topic):
        self.kubo_client = client
        self.topic = topic
        self.my_id = None             # PeerID, set in start()
        self.my_asn = None          # ASN of this peer's ISP
        self.peers = {}             # peer_id -> {"status", "asn", "last_seen"}
        self.status = FREE          # free -> pairing -> paired
        self.partner = None           # partner's PeerID
        self.role = None              # 'client' or 'server'
        self._handlers = {}           # kind -> async handler(data)
        self.paired = asyncio.Event() # set once the pair is defined
        self._tasks = []

    # --- Public API ----------------------------------------------------------

    # registers an async handler for a message type (e.g. 'offer')
    def on(self, kind, handler):
        self._handlers[kind] = handler


    async def start(self):
        self.my_id = await self.kubo_client.id()
        self.my_asn = get_asn()
        self._tasks.append(asyncio.create_task(self._consume()))
        self._tasks.append(asyncio.create_task(self._heartbeat()))
        self._tasks.append(asyncio.create_task(self._cleaner()))


    # sends an addressed message (e.g. offer/answer) to the partner
    async def send(self, kind, to, extra):
        payload = {"kind": kind, "to": to}
        payload.update(extra)
        await self._publish(payload)


    async def close(self):
        for task in self._tasks:
            task.cancel()

    # --- Internal loops ------------------------------------------------------

    async def _heartbeat(self):
        # always announces, with the current status
        while True:
            await self._publish({"kind": "announce", "status": self.status, "asn": self.my_asn})
            await asyncio.sleep(HEARTBEAT_INTERVAL)


    async def _consume(self):
        async for message in self.kubo_client.pubsub_sub(self.topic):
            # an exception here would silently kill the consumer task
            try:
                data = json.loads(message["data"])
                if data.get("from") == self.my_id:
                    continue  # ignores its own echo
                await self._dispatch(data)
            except Exception:
                logger.exception("handler de sinalização estourou; ignorando a mensagem e seguindo")


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


    async def _cleaner(self): # removes disconnected peers from the local list
        while True:
            await asyncio.sleep(PEER_TIMEOUT)
            now = time.monotonic()
            disconnected = []

            for id, p in self.peers.items():
                if now - p["last_seen"] > PEER_TIMEOUT:
                    disconnected.append(id)

            for id in disconnected:
                del self.peers[id]
                logger.info("par %s sumiu do tópico (sem announce há %ss)", id, PEER_TIMEOUT)


    # --- Matchmaking ---------------------------------------------------------

    async def _on_announce(self, data):
        other_peer_id = data["from"]
        # logs only when the peer first appears (announces arrive every 2s)
        if other_peer_id not in self.peers:
            logger.info("par %s visto no tópico (status=%s, asn=%s)",
                        other_peer_id, data.get("status"), data.get("asn"))
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
            logger.info("pedindo pareamento a %s", partner)
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
        return min(candidates)


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
        self._reset_to_free()


    def _reset_to_free(self):
        self.status = FREE
        self.partner = None
        self.role = None


    def reset_for_new_round(self):
        self._reset_to_free()
        self.paired.clear()


    async def _pair_timeout(self, expected_partner):
        await asyncio.sleep(PAIR_TIMEOUT)
        # if still waiting for the same peer, give up and go back to free
        if self.status == PAIRING and self.partner == expected_partner:
            logger.warning("%s não respondeu o pedido de pareamento em %ss", expected_partner, PAIR_TIMEOUT)
            self._reset_to_free()


    async def _finish_pairing(self):
        self.paired.set()
        if "role_defined" in self._handlers and self.role == CLIENT:
            await self._handlers["role_defined"](self.partner)

    # --- Helper --------------------------------------------------------------

    async def _publish(self, payload):
        payload["from"] = self.my_id
        await self.kubo_client.pubsub_pub(self.topic, json.dumps(payload))
