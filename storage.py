import os
import logging
import pandas as pd
from influx_service import InfluxService
from custom_types import Results

logger = logging.getLogger(__name__)

# o que identifica a medição (vira tag/índice no influx); todo o resto do results é métrica (field)
TAG_KEYS = ("role", "ip", "candidate_type")

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


def save_to_file(results: Results):
    results_data_frame = pd.DataFrame([results]).rename(columns=_column_with_unit)
    file_exists = os.path.exists('results.csv')
    results_data_frame.to_csv("results.csv", mode='a', header=not file_exists, index=False)
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
