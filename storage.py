import os
import pandas as pd
from influx_service import InfluxService
from custom_types import Results

def save_to_file(results: Results):
    if "package_loss" in results and results["package_loss"] is not None:
        results = dict(results) #copiei o dicionário pra nao modificar o original
        for key in ["latency", "upload", "download"]: #TODO adicionar a coluna de test_size no influx
            results.pop(key, None)

        results_data_frame = pd.DataFrame([results]).rename(columns={
            "package_loss": "package loss (%)",
        })
        key_fields = ["package_loss"]

    else:
        results_data_frame = pd.DataFrame([results]).rename(columns={
            "latency": "latency (ms)",
            "upload": "upload (Mbps)",
            "download": "download (Mbps)",
        })
        key_fields = ["latency", "upload", "download"]

    file_exists = os.path.exists('results.csv')
    results_data_frame.to_csv("results.csv", mode='a', header=not file_exists, index=False)
    save_to_db(results, key_fields)


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
