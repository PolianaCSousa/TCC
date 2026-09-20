import asyncio
import json
import logging
from aiortc.exceptions import InvalidStateError
from state import state

logger = logging.getLogger(__name__)


def safe_send(channel, data) -> bool:
    """Envia só se o canal ainda estiver aberto. Devolve se o envio aconteceu.

    Quando a conexão cai, as corrotinas presas em event_timeout acordam depois e
    tentam enviar num canal já morto — o aiortc levanta InvalidStateError e o pyee
    transforma isso em exceção não tratada no event loop.
    """
    if channel is None or channel.readyState != "open":
        logger.debug("send ignorado: canal %s", getattr(channel, "readyState", "inexistente"))
        return False
    try:
        channel.send(data)
        return True
    except InvalidStateError:
        # corrida: o canal fechou entre o readyState e o send
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
                p.cancel()  # confirmar na documentação se o cancel mata a task imediatamente ou se ela demora um pouco e continua rodando até que ela perceba que foi cancelada

            return name


def update_peers_list(this, role, server_peers): 
    for peer in server_peers:
        if peer["sid"] == this["target"]:
            peer["role"] = role
            peer["status"] = "OCCUPIED"
            peer["target"] = state.sid