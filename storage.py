import os
import pandas as pd
from influx_service import InfluxService
from custom_types import Results

def save_to_file(results: Results):
    # 1 linha por experimento: o dict já vem completo com todas as colunas (prefixadas por tamanho).
    results_data_frame = pd.DataFrame([results])
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
