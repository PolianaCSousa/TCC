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

# A hora UTC entra na chave de rendezvous: peers que sobem na mesma hora derivam
# o mesmo CID e se acham, enquanto registros de containers ja mortos ficam presos
# em horas antigas e somem sozinhos da busca.
BUCKET_FORMAT = "%Y-%m-%dT%H"

# no stream do routing/findprovs, cada linha traz um Type: 4 e a unica que carrega
# "fulano tem esse conteudo". As outras sao progresso da caminhada na DHT.
PROVIDER_TYPE = 4


def bucket_key(topic, when):
    """Texto determinístico que vira o CID de encontro daquela hora."""
    em_utc = when.astimezone(timezone.utc)
    return f"{topic}|{em_utc.strftime(BUCKET_FORMAT)}"


def search_keys(topic, when):
    """Chaves a procurar: a hora atual e a anterior, pra não furar na virada."""
    return [bucket_key(topic, when), bucket_key(topic, when - timedelta(hours=1))]


class DialPolicy:
    """Decide em quem discar. Segura a insistência em peer que não responde:
    registro fantasma na DHT sobrevive por horas depois que o container morreu,
    e cada tentativa nele custa ~13s."""

    def __init__(self, base_backoff, max_backoff):
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.failures = {}   # peer_id -> quantidade de falhas seguidas
        self.retry_at = {}   # peer_id -> instante (monotonic) em que volta a ser alvo

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


def direct_addrs(addrs):
    """Fica só com os endereços discáveis diretamente.

    Endereço de circuit relay fecha conexão, mas conexão de relay é limitada em
    tempo e bytes, e o gossipsub não abre stream nela — ou seja, o swarm connect
    responde "success" e o pareamento mesmo assim nunca acontece.
    """
    return [addr for addr in addrs if "/p2p-circuit" not in addr]


def needs_dial(conexao_atual, addrs_conhecidos):
    """Decide se vale discar neste peer.

    Conexão só por relay conta como não-pareado: ela aparece no swarm peers, mas
    o gossipsub não roda nela. Só que insistir também não adianta se o peer não
    publicou nenhum endereço direto — o redial produziria outro relay.
    """
    if conexao_atual is None:
        return True
    if direct_addrs(conexao_atual):
        return False
    return bool(direct_addrs(addrs_conhecidos))


def parse_provider_ids(lines):
    """Só os PeerIDs de quem anunciou o conteúdo."""
    return [provider["id"] for provider in parse_providers(lines)]


def parse_providers(lines):
    """Lê o stream do findprovs e devolve quem anunciou, com seus endereços."""
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
            continue  # stream cortado no meio não pode derrubar o daemon
        if entrada.get("Type") != PROVIDER_TYPE:
            continue
        for resposta in entrada.get("Responses") or []:
            peer_id = resposta.get("ID")
            if not peer_id:
                continue
            if peer_id not in por_id:
                por_id[peer_id] = {"id": peer_id, "addrs": []}
                encontrados.append(por_id[peer_id])
            # a DHT devolve o mesmo provider por caminhos diferentes, às vezes com
            # conjuntos de endereço diferentes: juntar tudo aumenta a chance de
            # sobrar um endereço direto
            for addr in resposta.get("Addrs") or []:
                if addr not in por_id[peer_id]["addrs"]:
                    por_id[peer_id]["addrs"].append(addr)
    return encontrados


class SwarmConnector:
    """Daemon de pareamento: usa a DHT do IPFS como ponto de encontro.

    Cada peer anuncia "eu tenho o CID da hora atual" e pergunta quem mais
    anunciou o mesmo. O conteúdo em si não importa — é só uma coordenada
    combinada. Quem aparecer é outro peer rodando esta ferramenta, e discar
    nele forma o mesh do gossipsub, que é o que faltava pro announce chegar.
    """

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
        self._cids = {}          # chave de rendezvous -> CID (evita re-add a cada rodada)
        self._tasks = []
        self._started_at = None  # pra medir quanto demorou até o primeiro encontro
        self._first_contact_logged = False

    # --- API pública ---------------------------------------------------------

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

    # --- Uma rodada de cada coisa --------------------------------------------

    async def announce_once(self, when):
        """Publica na DHT que este peer está no encontro desta hora."""
        cid = await self._cid_for(bucket_key(self.topic, when))
        await self.kubo.routing_provide(cid)
        return cid

    async def discover_once(self, when):
        """Procura, disca e devolve os peers com quem a conexão subiu agora."""
        candidatos = {}
        for chave in search_keys(self.topic, when):
            try:
                cid = await self._cid_for(chave)
                linhas = await self.kubo.routing_findprovs(
                    cid, num_providers=SWARM_NUM_PROVIDERS, timeout=SWARM_FINDPROVS_TIMEOUT
                )
            except Exception as erro:
                # DHT instável ou API fora do ar não pode matar o daemon:
                # a próxima rodada tenta de novo
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

    async def next_interval(self):
        """Sozinho no tópico, procura rápido; com companhia, relaxa."""
        try:
            no_topico = await self.kubo.pubsub_peers(self.topic)
        except Exception as erro:
            logger.warning("pubsub/peers falhou: %s", erro)
            return self.interval_alone
        return self.interval_together if no_topico else self.interval_alone

    # --- Interno -------------------------------------------------------------

    async def _dial_all(self, alvos):
        # em paralelo de propósito: um registro fantasma leva ~13s pra falhar, e
        # em série isso atrasaria o peer vivo que está na fila atrás dele
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
                    # sem endereço direto a conexão sai por relay, e relay não
                    # carrega o gossipsub. Não é motivo de alarme: basta um peer
                    # alcançável no tópico pra repassar a fofoca entre os dois.
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
        # número pra comparar antes/depois do daemon
        logger.info("primeiro encontro em %.1fs desde o start do daemon",
                    time.monotonic() - self._started_at)

    async def _cid_for(self, chave):
        """CID daquela chave de rendezvous.

        Quem calcula é o próprio Kubo, não a gente: o CID precisa bater byte a
        byte entre peers, e reimplementar o multihash aqui seria uma fonte de
        divergência silenciosa.
        """
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
