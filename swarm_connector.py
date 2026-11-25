import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from constants import (
    SWARM_BASE_BACKOFF,
    SWARM_DIAL_TIMEOUT,
    SWARM_FINDPROVS_TIMEOUT,
    SWARM_INTERVAL_ALONE,
    SWARM_INTERVAL_TOGETHER,
    SWARM_MAX_BACKOFF,
    SWARM_NUM_PROVIDERS,
    SWARM_PROVIDE_INTERVAL,
)

logger = logging.getLogger(__name__)

# the UTC hour is part of the key: peers active in the same hour derive the same CID
BUCKET_FORMAT = "%Y-%m-%dT%H"

# in the routing/findprovs stream, Type 4 is the line that carries the providers
PROVIDER_TYPE = 4


# deterministic text that becomes the rendezvous CID for that hour
def bucket_key(topic, when):
    em_utc = when.astimezone(timezone.utc)
    return f"{topic}|{em_utc.strftime(BUCKET_FORMAT)}"


# keys to search: the current hour and the previous one, to cover the hour rollover
def search_keys(topic, when):
    return [bucket_key(topic, when), bucket_key(topic, when - timedelta(hours=1))]


# decides whom to dial, with backoff for peers that do not respond
class DialPolicy:
    def __init__(self, base_backoff, max_backoff):
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.failures = {}   # peer_id -> number of consecutive failures
        self.retry_at = {}   # peer_id -> time (monotonic) when it becomes a target again

    def targets(self, provider_ids, my_id, connected_ids, now):
        escolhidos = []
        for peer_id in provider_ids:
            if peer_id == my_id or peer_id in connected_ids:
                continue
            if now < self.retry_at.get(peer_id, 0):
                continue
            escolhidos.append(peer_id)
        return escolhidos

    def record_failure(self, peer_id, now):
        falhas = self.failures.get(peer_id, 0) + 1
        self.failures[peer_id] = falhas
        espera = min(self.base_backoff * (2 ** (falhas - 1)), self.max_backoff)
        self.retry_at[peer_id] = now + espera

    def record_success(self, peer_id):
        self.failures.pop(peer_id, None)
        self.retry_at.pop(peer_id, None)


# keeps only the directly dialable addresses (no circuit relay)
def direct_addrs(addrs):
    return [addr for addr in addrs if "/p2p-circuit" not in addr]


# decides whether to dial this peer: a relay-only connection counts as not paired
def needs_dial(conexao_atual, addrs_conhecidos):
    if conexao_atual is None:
        return True
    if direct_addrs(conexao_atual):
        return False
    return bool(direct_addrs(addrs_conhecidos))


# only the PeerIDs of those who announced the content
def parse_provider_ids(lines):
    return [provider["id"] for provider in parse_providers(lines)]


# reads the findprovs stream and returns who announced, with their addresses
def parse_providers(lines):
    encontrados = []
    por_id = {}
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line:
            continue
        try:
            entrada = json.loads(line)
        except json.JSONDecodeError:
            continue  # truncated stream line
        if entrada.get("Type") != PROVIDER_TYPE:
            continue
        for resposta in entrada.get("Responses") or []:
            peer_id = resposta.get("ID")
            if not peer_id:
                continue
            if peer_id not in por_id:
                por_id[peer_id] = {"id": peer_id, "addrs": []}
                encontrados.append(por_id[peer_id])
            # the DHT may return the same provider with different addresses: merge them all
            for addr in resposta.get("Addrs") or []:
                if addr not in por_id[peer_id]["addrs"]:
                    por_id[peer_id]["addrs"].append(addr)
    return encontrados


# pairing daemon: uses the IPFS DHT as the meeting point
class SwarmConnector:
    def __init__(self, kubo, topic,
                 interval_alone=SWARM_INTERVAL_ALONE,
                 interval_together=SWARM_INTERVAL_TOGETHER,
                 provide_interval=SWARM_PROVIDE_INTERVAL):
        self.kubo = kubo
        self.topic = topic
        self.interval_alone = interval_alone
        self.interval_together = interval_together
        self.provide_interval = provide_interval
        self.my_id = None
        self.policy = DialPolicy(SWARM_BASE_BACKOFF, SWARM_MAX_BACKOFF)
        self._cids = {}          # rendezvous key -> CID
        self._tasks = []
        self._started_at = None
        self._first_contact_logged = False

    # --- Public API ----------------------------------------------------------

    async def start(self):
        await self.resolve_identity()
        self._started_at = time.monotonic()
        self._tasks.append(asyncio.create_task(self._announce_loop()))
        self._tasks.append(asyncio.create_task(self._discover_loop()))

    async def close(self):
        for task in self._tasks:
            task.cancel()

    async def resolve_identity(self):
        self.my_id = await self.kubo.id()

    # --- One round of each ---------------------------------------------------

    # publishes on the DHT that this peer is in this hour's rendezvous
    async def announce_once(self, when):
        cid = await self._cid_for(bucket_key(self.topic, when))
        await self.kubo.routing_provide(cid)
        return cid

    # searches, dials and returns the peers whose connection just came up
    async def discover_once(self, when):
        candidatos = {}
        for chave in search_keys(self.topic, when):
            try:
                cid = await self._cid_for(chave)
                linhas = await self.kubo.routing_findprovs(
                    cid, num_providers=SWARM_NUM_PROVIDERS, timeout=SWARM_FINDPROVS_TIMEOUT
                )
            except Exception as erro:
                logger.warning("busca na DHT por %s falhou: %s", chave, erro)
                continue
            for provider in parse_providers(linhas):
                conhecido = candidatos.setdefault(provider["id"], [])
                for addr in provider["addrs"]:
                    if addr not in conhecido:
                        conhecido.append(addr)

        if not candidatos:
            return []

        try:
            conexoes = await self.kubo.swarm_peers()
        except Exception as erro:
            logger.warning("swarm/peers falhou: %s", erro)
            return []

        resolvidos = {
            peer_id
            for peer_id, addrs in candidatos.items()
            if not needs_dial(conexoes.get(peer_id), addrs)
        }
        alvos = self.policy.targets(
            list(candidatos), self.my_id, resolvidos, time.monotonic()
        )
        if not alvos:
            return []
        return await self._dial_all({alvo: candidatos[alvo] for alvo in alvos})

    # alone in the topic, search fast; with company, slow down
    async def next_interval(self):
        try:
            no_topico = await self.kubo.pubsub_peers(self.topic)
        except Exception as erro:
            logger.warning("pubsub/peers falhou: %s", erro)
            return self.interval_alone
        return self.interval_together if no_topico else self.interval_alone

    # --- Internal ------------------------------------------------------------

    async def _dial_all(self, alvos):
        # in parallel: an unreachable peer takes ~13s to fail
        resultados = await asyncio.gather(
            *[
                self.kubo.swarm_connect(alvo, direct_addrs(addrs), timeout=SWARM_DIAL_TIMEOUT)
                for alvo, addrs in alvos.items()
            ],
            return_exceptions=True,
        )
        conectados = []
        agora = time.monotonic()
        for alvo, resultado in zip(alvos, resultados):
            if resultado is True:
                self.policy.record_success(alvo)
                conectados.append(alvo)
                if not direct_addrs(alvos[alvo]):
                    logger.info(
                        "%s só anuncia endereço de relay: esta conexão não forma mesh, "
                        "o pareamento depende de outro peer alcançável no tópico", alvo)
            else:
                self.policy.record_failure(alvo, agora)
        if conectados:
            self._log_first_contact(conectados)
        return conectados

    def _log_first_contact(self, conectados):
        logger.info("swarm connect em %d peer(s): %s", len(conectados), ", ".join(conectados))
        if self._first_contact_logged or self._started_at is None:
            return
        self._first_contact_logged = True
        logger.info("primeiro encontro em %.1fs desde o start do daemon",
                    time.monotonic() - self._started_at)

    # CID of the rendezvous key, computed by Kubo itself so it is identical across peers
    async def _cid_for(self, chave):
        if chave not in self._cids:
            self._cids[chave] = await self.kubo.add_bytes(chave.encode("utf-8"))
        return self._cids[chave]

    async def _announce_loop(self):
        while True:
            try:
                await self.announce_once(datetime.now(timezone.utc))
            except Exception as erro:
                logger.warning("anúncio na DHT falhou: %s", erro)
            await asyncio.sleep(self.provide_interval)

    async def _discover_loop(self):
        while True:
            try:
                await self.discover_once(datetime.now(timezone.utc))
            except Exception as erro:
                logger.warning("rodada de descoberta falhou: %s", erro)
            await asyncio.sleep(await self.next_interval())
