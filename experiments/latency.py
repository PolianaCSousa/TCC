from constants import (LAT_ACK, LAT, ACK, END_ITERATION, LATENCY, THROUGHPUT_LABELS)
import time
from utils import events_timeout, event_timeout, safe_send
import logging
from state import state
from statistics import mean, pstdev

logger = logging.getLogger(__name__)

# region Calculate and send latency package
async def server_send_lat_ack(latency_channel):
    state.server[state.t0_latency_key()].append(time.monotonic_ns())
    safe_send(latency_channel, LAT_ACK)
# endregion


async def client_send_lat_package(latency_channel):
    state.client[state.t0_latency_key()].append(time.monotonic_ns())
    safe_send(latency_channel, LAT)


async def client_send_ack(latency_channel):
    safe_send(latency_channel, ACK)

async def handle_server_latency_timeout(control_channel, timeout):
    response = await events_timeout({"ack_received": state.events["ack_received"],
                                     "lat_ack_error": state.events["lat_ack_error"]
                                     }, timeout)
    if response != "ack_received":
        t0 = state.server[state.t0_latency_key()]
        t1 = state.server[state.t1_latency_key()]
        if len(t1) < len(t0):  # appends None only if this iteration is missing a t1
            t1.append(None)
            safe_send(control_channel, END_ITERATION)

def calc_latency(all_measures, result_key, test_size):

    latency_col = LATENCY if result_key == LATENCY else f"{THROUGHPUT_LABELS[test_size]}_loaded_latency"
    jitter_col = "jitter" if result_key == LATENCY else f"{THROUGHPUT_LABELS[test_size]}_loaded_jitter"

    # a measure <= 0 means a misaligned t0/t1 pair: it is discarded
    descartadas = [m for m in all_measures if m <= 0]
    if descartadas:
        logger.warning("%s: %s de %s medida(s) descartada(s) por par desalinhado "
                       "(delta <= 0).", latency_col, len(descartadas), len(all_measures))
        all_measures = [m for m in all_measures if m > 0]

    if len(all_measures) > 0:
        latency = round(mean(all_measures) / 10**6, 2) # ns -> ms
        jitter = round(pstdev(all_measures) / 10**6, 2)
        state.results[latency_col] = latency
        state.results[jitter_col] = jitter
    else:
        state.results[latency_col] = None
        state.results[jitter_col] = None