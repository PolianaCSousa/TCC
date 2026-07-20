import os
import pandas as pd
from influx_service import InfluxService
from custom_types import Results

def _column_with_unit(col: str) -> str:
    if col.endswith("_upload") or col.endswith("_download"):
        return f"{col} (Mbps)"
    if col == "latency" or col.endswith("_loaded_latency"):
        return f"{col} (ms)"
    if col == "package_loss":
        return f"{col} (%)"
    return col


def save_to_file(results: Results):
    results_data_frame = pd.DataFrame([results]).rename(columns=_column_with_unit)
    file_exists = os.path.exists('results.csv')
    results_data_frame.to_csv("results.csv", mode='a', header=not file_exists, index=False)
    #save_to_db(results)


def save_to_db(results: Results, key_fields):
    key_tags = ["role", "sid"]
    
    tags = dict()
    fields = dict()

    for key in key_tags:
        tags[key] = results[key]

    for key in key_fields:
        fields[key] = results[key]

    influx = InfluxService()
    influx.write_data(tags=tags, fields=fields)
