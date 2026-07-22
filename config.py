from dotenv import load_dotenv
from aiortc import RTCIceServer
import requests
import logging
import os

load_dotenv()
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_URL = os.getenv("INFLUXDB_URL")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")
INFLUXDB_MEASUREMENT=os.getenv("INFLUXDB_MEASUREMENT")
TURN_API_KEY = os.getenv("TURN_API_KEY")

GOOGLE_STUN = RTCIceServer(urls="stun:stun.l.google.com:19302")

logger = logging.getLogger(__name__)

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