import logging
import requests
from ipwhois import IPWhois
from ipwhois.exceptions import BaseIpwhoisException

logger = logging.getLogger(__name__)


def get_public_ip():
    response = requests.get("https://api.ipify.org?format=json", timeout=5)
    response.raise_for_status()
    return response.json()["ip"]


def get_asn():  
    try:
        ip = get_public_ip()
        result = IPWhois(ip).lookup_rdap()
        asn_number = result.get("asn") 
        if not asn_number:
            logger.warning("whois não retornou ASN para o IP %s", ip)
            return None
        asn = asn_number.split()[0]  # o split pega o primeiro ASN retornado
        return asn
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning("Falha ao descobrir o IP público (%s).", e)
        return None
    except BaseIpwhoisException as e:
        logger.warning("Falha no whois ao resolver o ASN (%s).", e)
        return None
