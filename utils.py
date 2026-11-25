import asyncio
import json
import logging
from aiortc.exceptions import InvalidStateError
from state import state
from constants import PAIRING_TIMEOUT_SECONDS, ROUND_WATCHDOG_SECONDS

logger = logging.getLogger(__name__)


# sends only if the channel is still open; returns whether the send happened
def safe_send(channel, data) -> bool:
    if channel is None or channel.readyState != "open":
        logger.debug("send ignorado: canal %s", getattr(channel, "readyState", "inexistente"))
        return False
    try:
        channel.send(data)
        return True
    except InvalidStateError:
        # race: the channel closed between the readyState check and the send
        logger.debug("send ignorado: canal fechou durante o envio")
        return False


def try_parse_json(message):
    try:
        return json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None
    

async def event_timeout(event, timeout):
    try:
        await asyncio.wait_for(event.wait(), timeout)
        return True
    except TimeoutError:
        return False
    
    
# sends `total` packets in batches of `batch`, with `pause` between them; returns how many were sent
async def send_paced(channel, package, total, batch, pause):
    enviados = 0
    for i in range(total):
        if not safe_send(channel, package):
            break
        enviados += 1
        if (i + 1) % batch == 0:
            await asyncio.sleep(pause)
    return enviados


# waits for the round to end: short deadline until connected, watchdog afterwards
async def wait_round_outcome():
    eventos = {
        "round_done": state.events["round_done"],
        "connection_lost": state.events["connection_lost"],
    }
    outcome = await events_timeout(eventos, PAIRING_TIMEOUT_SECONDS)
    if outcome != "timeout" or not state.round_active:
        return outcome
    return await events_timeout(eventos, ROUND_WATCHDOG_SECONDS)


async def events_timeout(events: dict[str, asyncio.Event], timeout: float | None = None):
    tasks = {}
    for name, event in events.items():
        tasks[name] = asyncio.create_task(event.wait())

    done, pending = await asyncio.wait(tasks.values(), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)

    if not done:
        for task in pending:
            task.cancel()
        return "timeout"

    for name, task in tasks.items():
        if task in done:
            events[name].clear()

            for p in pending:
                p.cancel()

            return name


def update_peers_list(this, role, server_peers): 
    for peer in server_peers:
        if peer["sid"] == this["target"]:
            peer["role"] = role
            peer["status"] = "OCCUPIED"
            peer["target"] = state.sid