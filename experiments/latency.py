from constants import (LAT_ACK, LAT, ACK, END_ITERATION, LATENCY, THROUGHPUT_LABELS)
import time
from utils import events_timeout, event_timeout
import logging
from state import state
from statistics import mean, pstdev

logger = logging.getLogger(__name__)

# region Calculate and send latency package
async def server_send_lat_ack(latency_channel):
    state.server[state.t0_latency_key()].append(time.time_ns())
    latency_channel.send(LAT_ACK)
    #logger.info(">>> enviei LAT_ACK")
# endregion


async def client_send_lat_package(latency_channel):
    state.client[state.t0_latency_key()].append(time.time_ns())
    latency_channel.send(LAT)
    #logger.info(">>> enviei LAT")


async def client_send_ack(latency_channel):
    latency_channel.send(ACK)
    #logger.info(">>> enviei ACK")

async def handle_server_latency_timeout(control_channel, timeout):
    # event_ocurred = await event_timeout
    response = await events_timeout({"ack_received": state.events["ack_received"],
                                     "lat_ack_error": state.events["lat_ack_error"]
                                     }, timeout)
    if response != "ack_received":
        t0 = state.server[state.t0_latency_key()]
        t1 = state.server[state.t1_latency_key()]
        if len(t1) < len(t0):          # só anexa None se estou devendo um t1 nesta iteração
            t1.append(None)
            control_channel.send(END_ITERATION)

def calc_latency(all_measures, result_key, test_size):

    latency_col = LATENCY if result_key == LATENCY else f"{THROUGHPUT_LABELS[test_size]}_loaded_latency"
    jitter_col = "jitter" if result_key == LATENCY else f"{THROUGHPUT_LABELS[test_size]}_loaded_jitter"

    if len(all_measures) > 0:
        latency = round(mean(all_measures) / 10**6, 2) #10**6 #converte de ns para ms
        jitter = round(pstdev(all_measures) / 10**6, 2)
        state.results[latency_col] = latency
        state.results[jitter_col] = jitter
    else:
        state.results[latency_col] = None
        state.results[jitter_col] = None