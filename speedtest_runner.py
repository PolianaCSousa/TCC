import asyncio
import json
import logging
import time
from custom_types import Results
from constants import COMPLETE, SPEEDTEST_TIMEOUT_SECONDS
from storage import save_speedtest

logger = logging.getLogger(__name__)

FAILED = "failed"

# the --accept-* flags skip the interactive license prompt on first run
SPEEDTEST_CMD = ("speedtest", "--format=json", "--accept-license", "--accept-gdpr")


def _bandwidth_to_Mbps(bandwidth_bytes_per_sec):
    # Ookla reports bandwidth in bytes per second
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
        # equivalent to loaded_latency
        "download_loaded_latency": download.get("latency", {}).get("iqm"),
        "upload_loaded_latency": upload.get("latency", {}).get("iqm"),
        "package_loss": saida.get("packetLoss"),
        "result_url": saida.get("result", {}).get("url"),
    }


def _failed(role: str | None, motivo: str) -> Results:
    logger.warning("Speedtest falhou: %s", motivo)
    return {"role": role, "status": FAILED, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# runs one Speedtest measurement and saves the result; never raises
async def run_speedtest(role: str | None):
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
        # with --format=json the output is one object per line; the result is the one with type "result"
        linhas = [json.loads(l) for l in stdout.decode().splitlines() if l.strip()]
        saida = next(l for l in linhas if l.get("type") == "result")
        resultado = _parse(saida, role)
    except (ValueError, StopIteration, KeyError) as e:
        save_speedtest(_failed(role, f"saída inesperada ({e}): {stdout[:300]!r}"))
        return

    logger.info("Speedtest: download %s Mbps, upload %s Mbps, latência %s ms (servidor %s)",
                resultado["download"], resultado["upload"], resultado["latency"], resultado["server_name"])
    save_speedtest(resultado)
