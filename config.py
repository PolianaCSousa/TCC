from dotenv import load_dotenv
from aiortc import RTCIceServer
from aioice import ice as aioice_ice
import math
import requests
import logging
import os

from constants import BYTES_THROUGHPUT_100MB, MIN_THROUGHPUT_BytePerSec

load_dotenv()
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_URL = os.getenv("INFLUXDB_URL")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")
INFLUXDB_MEASUREMENT=os.getenv("INFLUXDB_MEASUREMENT")
TURN_API_KEY = os.getenv("TURN_API_KEY")

GOOGLE_STUN = RTCIceServer(urls="stun:stun.l.google.com:19302")

logger = logging.getLogger(__name__)

# folga sobre a janela mínima, pra absorver variação de RTT e o tempo entre fases
_CONSENT_MARGEM = 1.25


def relax_ice_consent():
    """Afrouxa o consent freshness do ICE (RFC 7675) para a duração do experimento.

    O aioice manda um STUN binding request a cada 5s, com `retransmissions=0` — ou
    seja, 0,5s de prazo pra resposta — e fecha a conexão após 6 falhas seguidas.
    São ~30s de tolerância.

    O problema é que esses 0,5s são medidos na MESMA fila onde o teste de vazão
    está despejando dados. Saturar o link é o objetivo do teste, e a fila do
    gargalo inflada leva o RTT muito acima disso: em 2026-09-22 a loaded_latency
    medida chegou a 1024ms no teste de 100MB e 862ms no de 10MB, contra os 500ms
    de prazo. Resultado: o teste derrubava a própria conexão, e ~50% das rodadas
    morriam no meio.

    Afrouxar aqui não distorce medição nenhuma: o tráfego enviado continua
    idêntico, muda só o supervisor. E o supervisor está errado neste contexto — o
    consent freshness existe pra impedir que um endpoint seja usado pra inundar um
    terceiro que não quer o tráfego. Aqui as duas pontas são nossas e ambas querem
    os dados; o par nunca esteve ausente, só com a fila cheia por construção.

    A detecção de par realmente morto não se perde: continua nos timeouts de cada
    teste e no ROUND_WATCHDOG_SECONDS. Por isso a janela fica deliberadamente
    abaixo do watchdog — o ICE ainda percebe a queda, só que depois do experimento.
    """
    # pior caso que a própria ferramenta assume: 100MB no link mais lento que ela
    # espera atender. É o mesmo número que alimenta os timeouts em peer.py.
    janela = (BYTES_THROUGHPUT_100MB / MIN_THROUGHPUT_BytePerSec) * _CONSENT_MARGEM
    aioice_ice.CONSENT_FAILURES = math.ceil(janela / aioice_ice.CONSENT_INTERVAL)
    logger.info(
        "consent freshness do ICE afrouxado: %s falhas x %ss = %ss de tolerância",
        aioice_ice.CONSENT_FAILURES,
        aioice_ice.CONSENT_INTERVAL,
        aioice_ice.CONSENT_FAILURES * aioice_ice.CONSENT_INTERVAL,
    )

def get_connection_configuration():
    url = f'https://tccpoliana.metered.live/api/v1/turn/credentials?apiKey={TURN_API_KEY}'

    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        ice_servers = []

        for server in data:
            ice_servers.append(RTCIceServer(
                urls=server.get("urls"),
                username=server.get("username"),
                credential=server.get("credential")
            ))
        ice_servers.insert(0, GOOGLE_STUN)

    except (requests.RequestException, ValueError) as e:
        logger.warning("Falha ao obter TURN (%s). Seguindo só com STUN do Google.", e)
        ice_servers = [GOOGLE_STUN]

    return ice_servers