import aiohttp
import asyncio
import base64
import json
import logging

logger = logging.getLogger(__name__)


class KuboClient:
    def __init__(self, api_url="http://127.0.0.1:5001"):
        self.api_url = api_url
        self.session = None

    async def start(self):
        self.session = aiohttp.ClientSession()

    async def id(self):
        async with self.session.post(self.api_url + "/api/v0/id") as response:
            data = await response.json()
            return data["ID"]

    async def pubsub_pub(self, topic, data):
        encoded_topic = self.encode_topic(topic)
        params = { "arg": encoded_topic }
        form = aiohttp.FormData()
        form.add_field("file", data.encode("utf-8"), filename="file")

        async with self.session.post(
            self.api_url + "/api/v0/pubsub/pub",
            params=params,
            data=form
        ) as response:
            if response.status >= 400:
                body = await response.text()
                raise RuntimeError(
                    f'pubsub/pub no tópico {topic} falhou ({response.status}): {body}'
                )
            logger.debug("pubsub/pub %s -> %s", topic, response.status)


    async def pubsub_sub(self, topic):
        encoded_topic = self.encode_topic(topic)
        params = { "arg": encoded_topic }
            # o timeout padrao do aiohttp e total=300s, o que mataria esse stream aos
        # 5 minutos: a inscricao precisa ficar aberta indefinidamente.
        async with self.session.post(
            self.api_url + "/api/v0/pubsub/sub",
            params=params,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30),
        ) as response:
            async for line in response.content:
                line = line.strip()
                if not line:
                    continue
                message = json.loads(line)
                message["data"] = self.decode_data(message["data"])
                yield message
    


    # --- swarm / DHT (usados pelo swarm_connector) ---------------------------

    async def add_bytes(self, data):
        """Grava um bloco no blockstore local e devolve o CID.

        Os parâmetros são fixos de propósito: o CID precisa sair idêntico em
        todos os peers a partir do mesmo conteúdo, independentemente de como
        cada voluntário configurou o Kubo dele.
        """
        params = {
            "cid-version": "1",
            "raw-leaves": "true",
            "hash": "sha2-256",
            "chunker": "size-262144",
            "pin": "false",
        }
        form = aiohttp.FormData()
        form.add_field("file", data, filename="file")

        async with self.session.post(
            self.api_url + "/api/v0/add", params=params, data=form
        ) as response:
            payload = json.loads(await response.text())
            self._raise_if_error(payload, "add")
            return payload["Hash"]

    async def routing_provide(self, cid):
        """Anuncia na DHT que este peer tem esse CID.

        Exige o bloco no blockstore local — sem isso o Kubo recusa. E ele
        enfileira o trabalho e responde na hora: o registro leva mais alguns
        segundos até chegar aos nós da DHT.
        """
        async with self.session.post(
            self.api_url + "/api/v0/routing/provide", params={"arg": cid}
        ) as response:
            corpo = await response.text()
            for linha in corpo.splitlines():
                linha = linha.strip()
                if linha:
                    self._raise_if_error(json.loads(linha), "routing/provide")

    async def routing_findprovs(self, cid, num_providers, timeout):
        """Pergunta à DHT quem anunciou esse CID; devolve as linhas cruas do stream.

        A busca pode varrer a DHT por muito tempo quando não há o que achar, então
        o timeout não é erro: é o ponto de desistir e usar o que já chegou.
        """
        linhas = []
        try:
            await asyncio.wait_for(
                self._stream_findprovs(cid, num_providers, linhas), timeout
            )
        except asyncio.TimeoutError:
            logger.debug("findprovs %s: timeout de %ss, %d linhas colhidas", cid, timeout, len(linhas))
        return linhas

    async def _stream_findprovs(self, cid, num_providers, linhas):
        params = {"arg": cid, "num-providers": str(num_providers)}
        async with self.session.post(
            self.api_url + "/api/v0/routing/findprovs",
            params=params,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30),
        ) as response:
            async for line in response.content:
                linhas.append(line.decode("utf-8", errors="replace"))

    async def swarm_connect(self, peer_id, addrs, timeout):
        """Disca no peer pelos endereços dados; devolve True se a conexão subiu.

        Passar os endereços importa: com `/p2p/<id>` puro o Kubo escolhe sozinho e
        costuma acomodar num circuit relay, que é conexão limitada e não carrega o
        gossipsub. Sem endereço direto conhecido, o relay é melhor que nada — o
        DCUtR ainda pode promover a conexão a direta depois.
        """
        if addrs:
            alvos = [f"{addr}/p2p/{peer_id}" for addr in addrs]
        else:
            alvos = [f"/p2p/{peer_id}"]
        try:
            async with self.session.post(
                self.api_url + "/api/v0/swarm/connect",
                params=[("arg", alvo) for alvo in alvos],
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                payload = json.loads(await response.text())
                if payload.get("Type") == "error":
                    logger.debug("swarm/connect %s falhou: %s", peer_id, payload.get("Message"))
                    return False
                return True
        except (asyncio.TimeoutError, aiohttp.ClientError) as erro:
            logger.debug("swarm/connect %s falhou: %s", peer_id, erro)
            return False

    async def swarm_peers(self):
        """Conexões libp2p abertas agora: PeerID -> endereços por onde ele está ligado.

        Os endereços importam porque conexão via relay não vale como pareamento,
        e sem eles não dá pra distinguir uma da outra.
        """
        async with self.session.post(self.api_url + "/api/v0/swarm/peers") as response:
            payload = json.loads(await response.text())
            self._raise_if_error(payload, "swarm/peers")
            conexoes = {}
            for p in payload.get("Peers") or []:
                conexoes.setdefault(p["Peer"], []).append(p["Addr"])
            return conexoes

    async def pubsub_peers(self, topic):
        """Peers que o gossipsub enxerga NESTE tópico — o sinal de que o
        pareamento tem com quem acontecer."""
        params = {"arg": self.encode_topic(topic)}
        async with self.session.post(
            self.api_url + "/api/v0/pubsub/peers", params=params
        ) as response:
            payload = json.loads(await response.text())
            self._raise_if_error(payload, "pubsub/peers")
            return payload.get("Strings") or []

    def _raise_if_error(self, payload, endpoint):
        """A API do Kubo responde 200 e põe o erro no corpo nesses endpoints,
        então checar o status HTTP não basta."""
        if isinstance(payload, dict) and payload.get("Type") == "error":
            raise RuntimeError(f'{endpoint} falhou: {payload.get("Message")}')

    # --- Helpers -------------------------------------------------------------

    def encode_topic(self, topic):
        encoded = base64.urlsafe_b64encode(topic.encode("utf-8")).decode("utf-8")
        without_padding = encoded.rstrip("=")
        encoded_topic = "u" + without_padding

        return encoded_topic

    def decode_data(self, data):
        without_prefix = data[1:]
        with_padding = without_prefix + "=" * (-len(without_prefix) % 4)
        decoded_data = base64.urlsafe_b64decode(with_padding).decode("utf-8")

        return decoded_data


    async def close(self):
        await self.session.close()