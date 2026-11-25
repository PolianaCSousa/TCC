import os
import logging
import time
import pandas as pd
from influx_service import InfluxService
from custom_types import Results

logger = logging.getLogger(__name__)

RESULTS_FILE = "results.csv"
SPEEDTEST_FILE = "speedtest.csv"
SPEEDTEST_MEASUREMENT = "speedtest_results"

# influx tags (identify the measurement); everything else becomes a field
TAG_KEYS = ("role", "peer_id", "remote_peer_id", "ip", "candidate_type", "remote_candidate_type",
            "via_turn", "status")
SPEEDTEST_TAG_KEYS = ("role", "ip", "status", "isp", "server_id", "server_name", "server_location")
SPEEDTEST_CSV_ONLY = ("timestamp", "result_url")

_influx: InfluxService | None = None


def _column_with_unit(col: str) -> str:
    if col.endswith("_upload") or col.endswith("_download") or col in ("upload", "download"):
        return f"{col} (Mbps)"
    if col == "latency" or col.endswith("_loaded_latency"):
        return f"{col} (ms)"
    if col == "jitter" or col.endswith("_loaded_jitter"):
        return f"{col} (ms)"
    if col.endswith("package_loss"):
        return f"{col} (%)"
    return col


# if the existing CSV has a different header, archives it before writing
def _rotate_if_header_changed(columns: list[str], path: str = RESULTS_FILE):
    if not os.path.exists(path):
        return
    with open(path, newline="") as arquivo:
        cabecalho = arquivo.readline().strip()
    if cabecalho == ",".join(columns):
        return
    base, ext = os.path.splitext(path)
    antigo = f"{base}_{time.strftime('%Y%m%d-%H%M%S')}{ext}"
    os.rename(path, antigo)
    logger.warning("Colunas do %s mudaram. Arquivo anterior salvo como %s.", path, antigo)


def _append_csv(results: Results, path: str):
    results_data_frame = pd.DataFrame([results]).rename(columns=_column_with_unit)
    _rotate_if_header_changed(list(results_data_frame.columns), path)
    file_exists = os.path.exists(path)
    results_data_frame.to_csv(path, mode='a', header=not file_exists, index=False)


def save_to_file(results: Results):
    carimbado = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **results}
    _append_csv(carimbado, RESULTS_FILE)
    save_to_db(results)


def save_speedtest(results: Results):
    completo = {key: results.get(key) for key in _SPEEDTEST_COLUMNS}
    _append_csv(completo, SPEEDTEST_FILE)
    tags = {key: completo[key] for key in SPEEDTEST_TAG_KEYS}
    fields = {key: value for key, value in completo.items()
              if key not in SPEEDTEST_TAG_KEYS and key not in SPEEDTEST_CSV_ONLY}
    try:
        _get_influx().write_data(tags=tags, fields=fields, measurement=SPEEDTEST_MEASUREMENT)
    except Exception as e:  # the csv is the main source - a database failure must not break the test
        logger.warning("Falha ao gravar o Speedtest no InfluxDB (%s). Salvo apenas no %s.", e, SPEEDTEST_FILE)


# fixed column order for speedtest.csv
_SPEEDTEST_COLUMNS = (
    "timestamp", "role", "status", "isp", "ip", "server_id", "server_name", "server_location",
    "latency", "jitter", "download", "upload", "download_loaded_latency", "upload_loaded_latency",
    "package_loss", "result_url",
)


def _get_influx() -> InfluxService:
    global _influx
    if _influx is None:  # reuses the client across rounds
        _influx = InfluxService()
    return _influx


def save_to_db(results: Results):
    tags = {key: results.get(key) for key in TAG_KEYS}
    fields = {key: value for key, value in results.items() if key not in TAG_KEYS}

    try:
        _get_influx().write_data(tags=tags, fields=fields)
    except Exception as e:  # the csv is the main source - a database failure must not break the test
        logger.warning("Falha ao gravar no InfluxDB (%s). Resultado salvo apenas no results.csv.", e)


def close_db():
    global _influx
    if _influx is not None:
        _influx.close()
        _influx = None
