from constants import (LAT_ACK, LAT, ACK, LATENCY, LOADED_LATENCY, END_ITERATION)
import time
from utils import events_timeout, event_timeout
import logging
from state import state

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
        if not state.results[LATENCY] or not state.results[LOADED_LATENCY]:
            # state.results[LATENCY] = None
            state.server[state.t1_latency_key()].append(None)
            control_channel.send(END_ITERATION)