import asyncio
import json



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