import os
import pandas as pd
from influx_service import InfluxService
from custom_types import Results

def save_to_file(results: Results):
    results_data_frame = pd.DataFrame([results])
    results_data_frame = results_data_frame.rename(columns={
        "latency": "latency (ms)",
        "upload": "upload (Mbps)",
        "download": "download (Mbps)",
    })

    file_exists = os.path.exists('results.csv')
    results_data_frame.to_csv("results.csv", mode='a', header=not file_exists, index=False)
    save_to_db(results)


def save_to_db(results: Results):
    key_tags = ["role", "sid"]
    key_fields = ["latency", "upload", "download"]
    tags = dict()
    fields = dict()

    for key in key_tags:
        tags[key] = results[key]

    for key in key_fields:
        fields[key] = results[key]

    influx = InfluxService()
    influx.write_data(tags=tags, fields=fields)
