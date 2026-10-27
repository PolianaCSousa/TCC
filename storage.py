import os
import logging
import time
import pandas as pd
from influx_service import InfluxService
from custom_types import Results

logger = logging.getLogger(__name__)

RESULTS_FILE = "results.csv"
SPEEDTEST_FILE = "speedtest.csv"
# série separada: não mistura com tests_results, que os dashboards já consultam
SPEEDTEST_MEASUREMENT = "speedtest_results"

# o que identifica a medição (vira tag/índice no influx); todo o resto do results é métrica (field)
# status é categórico e de baixa cardinalidade (complete/aborted), então cabe como tag
# os peer ids TÊM que ser tag: o write_data converte todo field com float(), e um texto
# ali derrubava a escrita da rodada inteira no influx
TAG_KEYS = ("role", "peer_id", "remote_peer_id", "ip", "candidate_type", "remote_candidate_type",
            "via_turn", "status")
# timestamp e url são texto e o influx já carimba o horário; ficam só no csv
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
    if col.endswith("package_loss"):   # a ociosa e as quatro sob carga
        return f"{col} (%)"
    return col


def _rotate_if_header_changed(columns: list[str], path: str = RESULTS_FILE):
    """Se o CSV existente tem outro cabeçalho, arquiva antes de escrever.

    Sem isso, um append com coluna nova (status) entra desalinhado e o arquivo inteiro
    fica ilegível pro pandas na hora da análise.
    """
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
    # mesmo formato UTC do speedtest.csv, pra cruzar as duas tabelas pela hora. Só no
    # csv: no influx o próprio ponto já é carimbado na escrita, no mesmo instante
    carimbado = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **results}
    _append_csv(carimbado, RESULTS_FILE)
    save_to_db(results)


def save_speedtest(results: Results):
    # mesmas colunas-base da ferramenta (download, upload, latency...) pra comparar direto
    completo = {key: results.get(key) for key in _SPEEDTEST_COLUMNS}
    _append_csv(completo, SPEEDTEST_FILE)
    tags = {key: completo[key] for key in SPEEDTEST_TAG_KEYS}
    fields = {key: value for key, value in completo.items()
              if key not in SPEEDTEST_TAG_KEYS and key not in SPEEDTEST_CSV_ONLY}
    try:
        _get_influx().write_data(tags=tags, fields=fields, measurement=SPEEDTEST_MEASUREMENT)
    except Exception as e:  # o csv é a fonte principal - falha no banco não pode derrubar o teste
        logger.warning("Falha ao gravar o Speedtest no InfluxDB (%s). Salvo apenas no %s.", e, SPEEDTEST_FILE)


# ordem fixa: a linha de falha tem só 3 chaves, e sem isto mudaria o cabeçalho e
# rotacionaria o speedtest.csv a cada falha
_SPEEDTEST_COLUMNS = (
    "timestamp", "role", "status", "isp", "ip", "server_id", "server_name", "server_location",
    "latency", "jitter", "download", "upload", "download_loaded_latency", "upload_loaded_latency",
    "package_loss", "result_url",
)


def _get_influx() -> InfluxService:
    global _influx
    if _influx is None:  # reaproveita o client entre as rodadas em vez de abrir um por teste
        _influx = InfluxService()
    return _influx


def save_to_db(results: Results):
    tags = {key: results.get(key) for key in TAG_KEYS}
    # pega todas as métricas do results, então coluna nova no csv já entra no influx sem mudar nada aqui
    fields = {key: value for key, value in results.items() if key not in TAG_KEYS}

    try:
        _get_influx().write_data(tags=tags, fields=fields)
    except Exception as e:  # o csv é a fonte principal - falha no banco não pode derrubar o teste
        logger.warning("Falha ao gravar no InfluxDB (%s). Resultado salvo apenas no results.csv.", e)


def close_db():
    global _influx
    if _influx is not None:
        _influx.close()
        _influx = None
