from dotenv import load_dotenv
from aiortc import RTCIceServer
from aioice import ice as aioice_ice
import math
import logging
import os

from constants import BYTES_THROUGHPUT_50MB, MIN_THROUGHPUT_BytePerSec

load_dotenv()
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_URL = os.getenv("INFLUXDB_URL")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")
INFLUXDB_MEASUREMENT=os.getenv("INFLUXDB_MEASUREMENT")

GOOGLE_STUN = RTCIceServer(urls="stun:stun.l.google.com:19302")

logger = logging.getLogger(__name__)

# margin over the minimum window
_CONSENT_MARGEM = 1.25


# relaxes ICE consent freshness (RFC 7675), which would expire while the test saturates the link
def relax_ice_consent():
    # worst case: 50MB over the slowest link the tool assumes
    janela = (BYTES_THROUGHPUT_50MB / MIN_THROUGHPUT_BytePerSec) * _CONSENT_MARGEM
    aioice_ice.CONSENT_FAILURES = math.ceil(janela / aioice_ice.CONSENT_INTERVAL)
    logger.info(
        "consent freshness do ICE afrouxado: %s falhas x %ss = %ss de tolerância",
        aioice_ice.CONSENT_FAILURES,
        aioice_ice.CONSENT_INTERVAL,
        aioice_ice.CONSENT_FAILURES * aioice_ice.CONSENT_INTERVAL,
    )

def get_connection_configuration():
    # public STUN only: without TURN, a peer behind a restrictive NAT cannot connect
    return [GOOGLE_STUN]