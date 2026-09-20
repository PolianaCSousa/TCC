import os
import logging
import time
import pandas as pd
from influx_service import InfluxService
from custom_types import Results

logger = logging.getLogger(__name__)

RESULTS_FILE = "results.csv"

# o que identifica a medição (vira tag/índice no influx); todo o resto do results é métrica (field)
# status é categórico e de baixa cardinalidade (complete/aborted), então cabe como tag
TAG_KEYS = ("role", "ip", "candidate_type", "status")

_influx: InfluxService | None = None


def _column_with_unit(col: str) -> str:
    if col.endswith("_upload") or col.endswith("_download"):
        return f"{col} (Mbps)"
    if col == "latency" or col.endswith("_loaded_latency"):
        return f"{col} (ms)"
    if col == "jitter" or col.endswith("_loaded_jitter"):
        return f"{col} (ms)"
    if col == "package_loss":
        return f"{col} (%)"
    return col


def _rotate_if_header_changed(columns: list[str]):
    """Se o CSV existente tem outro cabeçalho, arquiva antes de escrever.

    Sem isso, um append com coluna nova (status) entra desalinhado e o arquivo inteiro
    fica ilegível pro pandas na hora da análise.
    """
    if not os.path.exists(RESULTS_FILE):
        return
    with open(RESULTS_FILE, newline="") as arquivo:
        cabecalho = arquivo.readline().strip()
    if cabecalho == ",".join(columns):
        return
    antigo = f"results_{time.strftime('%Y%m%d-%H%M%S')}.csv"
    os.rename(RESULTS_FILE, antigo)
    logger.warning("Colunas do results.csv mudaram. Arquivo anterior salvo como %s.", antigo)


def save_to_file(results: Results):
    results_data_frame = pd.DataFrame([results]).rename(columns=_column_with_unit)
    _rotate_if_header_changed(list(results_data_frame.columns))
    file_exists = os.path.exists(RESULTS_FILE)
    results_data_frame.to_csv(RESULTS_FILE, mode='a', header=not file_exists, index=False)
    save_to_db(results)


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
