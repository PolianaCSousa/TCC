import asyncio
import json
import logging
from aiortc.exceptions import InvalidStateError
from state import state
from constants import PAIRING_TIMEOUT_SECONDS, ROUND_WATCHDOG_SECONDS

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
    
    
async def wait_round_outcome():
    """Espera a rodada acabar, com um prazo por fase.

    Duas situações moram nesta espera e pedem orçamentos que diferem em duas ordens
    de grandeza: "ainda não conectou" (normal ~6s) e "rodada em andamento" (legítimo
    até 20+ min). Um prazo só não atende às duas — foi o que deixou a rodada 5 de
    2026-09-26 muda por 45min com uma oferta sem resposta.

    Devolve o mesmo que events_timeout: "round_done", "connection_lost" ou "timeout".
    Se a conexão subir dentro do prazo curto, a segunda espera parte do zero com o
    watchdog inteiro — o total pode passar de ROUND_WATCHDOG por até PAIRING_TIMEOUT,
    e isso é aceitável para uma rede de segurança.
    """
    eventos = {
        "round_done": state.events["round_done"],
        "connection_lost": state.events["connection_lost"],
    }
    outcome = await events_timeout(eventos, PAIRING_TIMEOUT_SECONDS)
    if outcome != "timeout" or not state.round_active:
        return outcome
    # conectou dentro do prazo curto: agora vale o orçamento da rodada inteira
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
                p.cancel()  # confirmar na documentação se o cancel mata a task imediatamente ou se ela demora um pouco e continua rodando até que ela perceba que foi cancelada

            return name


def update_peers_list(this, role, server_peers): 
    for peer in server_peers:
        if peer["sid"] == this["target"]:
            peer["role"] = role
            peer["status"] = "OCCUPIED"
            peer["target"] = state.sid