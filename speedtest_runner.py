import asyncio
import json
import logging
import time
from custom_types import Results
from constants import COMPLETE, SPEEDTEST_TIMEOUT_SECONDS
from storage import save_speedtest

logger = logging.getLogger(__name__)

FAILED = "failed"

# CLI oficial da Ookla. Os dois --accept-* respondem sozinhos à licença e ao aviso de
# GDPR, que senão travam a primeira execução esperando um "YES" no terminal.
SPEEDTEST_CMD = ("speedtest", "--format=json", "--accept-license", "--accept-gdpr")


def _bandwidth_to_Mbps(bandwidth_bytes_per_sec):
    # a Ookla devolve bandwidth em BYTES por segundo; a ferramenta reporta em Mbps
    return round(bandwidth_bytes_per_sec * 8 / 10 ** 6, 2)


def _parse(saida: dict, role: str | None) -> Results:
    download = saida.get("download", {})
    upload = saida.get("upload", {})
    ping = saida.get("ping", {})
    server = saida.get("server", {})
    return {
        "role": role,
        "status": COMPLETE,
        "timestamp": saida.get("timestamp"),
        "isp": saida.get("isp"),
        "ip": saida.get("interface", {}).get("externalIp"),
        "server_id": server.get("id"),
        "server_name": server.get("name"),
        "server_location": server.get("location"),
        "latency": ping.get("latency"),
        "jitter": ping.get("jitter"),
        "download": _bandwidth_to_Mbps(download["bandwidth"]),
        "upload": _bandwidth_to_Mbps(upload["bandwidth"]),
        # latência medida durante o download/upload: é o equivalente da loaded_latency
        "download_loaded_latency": download.get("latency", {}).get("iqm"),
        "upload_loaded_latency": upload.get("latency", {}).get("iqm"),
        # nem todo servidor mede perda; ausente vira None e fica fora do influx
        "package_loss": saida.get("packetLoss"),
        "result_url": saida.get("result", {}).get("url"),
    }


def _failed(role: str | None, motivo: str) -> Results:
    # a falha também é gravada: dá pra contar quantas vezes o speedtest não rodou
    # sem confundir "não mediu" com "não tentou"
    logger.warning("Speedtest falhou: %s", motivo)
    return {"role": role, "status": FAILED, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


async def run_speedtest(role: str | None):
    """Roda uma medição do Speedtest e grava em speedtest.csv / influx.

    Nunca levanta exceção: o speedtest é a referência de comparação, e uma falha
    nele não pode derrubar o daemon da ferramenta.
    """
    logger.info("Iniciando Speedtest (Ookla)...")
    try:
        proc = await asyncio.create_subprocess_exec(
            *SPEEDTEST_CMD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        save_speedtest(_failed(role, "comando 'speedtest' não encontrado — o CLI da Ookla está instalado?"))
        return

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), SPEEDTEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        save_speedtest(_failed(role, f"passou de {SPEEDTEST_TIMEOUT_SECONDS}s sem terminar"))
        return

    if proc.returncode != 0:
        save_speedtest(_failed(role, f"saiu com código {proc.returncode}: {stderr.decode(errors='replace').strip()}"))
        return

    try:
        # com --format=json a saída é um objeto por linha; o resultado é o de type "result"
        linhas = [json.loads(l) for l in stdout.decode().splitlines() if l.strip()]
        saida = next(l for l in linhas if l.get("type") == "result")
        resultado = _parse(saida, role)
    except (ValueError, StopIteration, KeyError) as e:
        save_speedtest(_failed(role, f"saída inesperada ({e}): {stdout[:300]!r}"))
        return

    logger.info("Speedtest: download %s Mbps, upload %s Mbps, latência %s ms (servidor %s)",
                resultado["download"], resultado["upload"], resultado["latency"], resultado["server_name"])
    save_speedtest(resultado)
