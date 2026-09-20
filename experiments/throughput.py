from constants import (
    BYTES_PER_PACKAGE, 
    END_THROUGHPUT, 
    MIN_THROUGHPUT_BytePerSec, 
    CONTROL, 
    SHORT_TIMEOUT, 
    BYTES_THROUGHPUT_10MB,
    THROUGHPUT,
    BUFFER_AMOUNT_LIMIT,
    BUFFER_DRAIN_TIMEOUT,
    MAX_BUFFER_STALLS,
    UPLOAD_ERROR,
    UPLOAD_RECEIVED,
    END_TEST,
    THROUGHPUT_LABELS)
import logging
from state import state
import json
from utils import event_timeout, events_timeout, safe_send
from storage import save_to_file
import asyncio
import time

logger = logging.getLogger(__name__)

async def send_throughput_data(throughput_channel, control_channel, PEER, test_size):
    label = THROUGHPUT_LABELS[test_size]
    try:
        package = bytes(BYTES_PER_PACKAGE)
        PEER["qtd_total_bytes"] = test_size
        PEER["qtd_packages"] = 0
        qtd_pacotes = test_size // len(package)
        limite = BUFFER_AMOUNT_LIMIT[test_size]
        logger.info("%s: enviando %s pacotes de %s bytes", label, qtd_pacotes, len(package))

        for i in range(0, qtd_pacotes):
            if not safe_send(throughput_channel, package):
                logger.warning("%s: canal de vazão fechou no pacote %s/%s. Abortando o envio.",
                               label, i, qtd_pacotes)
                return False
            if not await _wait_buffer_drain(throughput_channel, limite, label, i, qtd_pacotes):
                return False
        safe_send(control_channel, END_THROUGHPUT)
        return True
    except Exception as e:
        logger.exception("Erro no envio dos dados da vazão: %s", e)
        return False


async def _wait_buffer_drain(throughput_channel, limite, label, pacote, qtd_pacotes):
    """Segura o envio até o buffer voltar abaixo do limite.

    O código antigo esperava UMA vez e seguia enfileirando mesmo sem drenar. Com
    100MB isso enche a fila do gargalo em segundos: o RTT passa dos 0,5s que o
    consent freshness do ICE (RFC 7675) tolera, o aioice acumula 6 falhas e fecha
    a conexão ~30s depois. Aqui a espera é um laço de verdade.
    """
    stalls = 0
    while throughput_channel.bufferedAmount > limite:
        state.events["throughput_buffer_drained"].clear()
        if throughput_channel.bufferedAmount <= limite:
            return True  # drenou entre a checagem e o clear
        if await event_timeout(state.events["throughput_buffer_drained"], BUFFER_DRAIN_TIMEOUT):
            stalls = 0
            continue
        stalls += 1
        logger.warning("%s: buffer parado em %s bytes há %ss (pacote %s/%s)",
                       label, throughput_channel.bufferedAmount,
                       stalls * BUFFER_DRAIN_TIMEOUT, pacote, qtd_pacotes)
        if stalls >= MAX_BUFFER_STALLS:
            logger.error("%s: buffer não drenou em %ss. Abortando o envio pra não derrubar a conexão.",
                         label, MAX_BUFFER_STALLS * BUFFER_DRAIN_TIMEOUT)
            return False
        if throughput_channel.readyState != "open":
            return False
    return True


async def calculate_throughput(role, PEER, throughput_finished, timeout=5):
    total_bytes_esperada = PEER[
        "qtd_total_bytes"]  ## ex.: teria o BYTES_THROUGHPUT_10MB como o valor dessa chave tam_bytes_test
    label = THROUGHPUT_LABELS[total_bytes_esperada]  
    timeout = total_bytes_esperada / MIN_THROUGHPUT_BytePerSec
    response = await event_timeout(throughput_finished, timeout)
    if response:
        PEER["t1_throughput"] = time.time()
        tempo = PEER["t1_throughput"] - PEER["t0_throughput"]
        vazao_em_bytes = ((PEER["qtd_packages"] - 1) * BYTES_PER_PACKAGE) / tempo  # 1400 é o tamanho do pacote
        vazao_em_MB = round(vazao_em_bytes / 10 ** 6, 2)
        vazao_em_Mbps = vazao_em_MB * 8
        if role == "server":
            state.results[f"{label}_download"] = vazao_em_Mbps
            safe_send(state.server["channels"][CONTROL], json.dumps({
                "msg": "upload",
                "value": vazao_em_Mbps,
                "test_size": total_bytes_esperada
            }))

            #logger.info("RESULTADO DO TESTE DE server.DOWNLOAD: \n A vazão calculada é de %s Mb/s para o tamanho de %s Mbytes", vazao_em_Mbps, int(PEER["qtd_total_bytes"])//10**6)
            

            await start_server_upload_timeout()
            await calculate_server_upload(state.server["qtd_total_bytes"])
        else:
            #logger.debug("sou cliente e ja tenho o download: %s Mbps", vazao_em_Mbps)
            state.results[f"{label}_download"] = vazao_em_Mbps  # It's here when the tests finish for client
            logger.info("Resultados do cliente: %s", state.results)
            safe_send(state.client["control_channel"], json.dumps({
                "msg": "upload",
                "value": vazao_em_Mbps,
                "test_size": total_bytes_esperada
            }))
    else:
        # meu download é none e o do outro par é none o upload
        logger.warning("%s: download expirou após %ss sem receber END_THROUGHPUT do par.", label, timeout)
        state.results[f"{label}_download"] = None
        if role == "server":
            safe_send(state.server["channels"][CONTROL], UPLOAD_ERROR)
        else:
            safe_send(state.client["control_channel"], UPLOAD_ERROR)

async def calculate_server_upload(test_size):
    state.server["channels"][THROUGHPUT].bufferedAmountLowThreshold = BUFFER_AMOUNT_LIMIT[test_size]
    await send_throughput_data(state.server["channels"][THROUGHPUT], state.server["channels"][CONTROL], state.server,
                                                     test_size)
            ## a task abaixo irá aguardar o evento upload_received ou upload_error
    await send_end_test(state.server["channels"][CONTROL], test_size / MIN_THROUGHPUT_BytePerSec, test_size)


async def start_server_upload_timeout():
    response = await event_timeout(state.events["start_server_upload"], SHORT_TIMEOUT)
    if response:
        safe_send(state.server["channels"][CONTROL], "Recebi ACK do upload do cliente. Vou iniciar o teste agora.")
    else:
        logger.warning("não recebi o ACK do upload do cliente em %ss. Iniciando o upload mesmo assim.", SHORT_TIMEOUT)
        safe_send(state.server["channels"][CONTROL],
                  "Não recebi o ACK do resultado do upload do cliente. Vou iniciar o teste mesmo assim.")


async def send_ack_end_upload(control_channel, timeout, test_size):
    label = THROUGHPUT_LABELS[test_size]
    response = await events_timeout({"upload_received": state.events["upload_received"],
                                     "upload_error": state.events["upload_error"]
                                     }, timeout)
    if response == "upload_received":
        safe_send(control_channel, UPLOAD_RECEIVED)
    else:
        logger.warning("%s: não recebi o resultado do meu upload (%s). Seguindo com upload=None.", label, response)
        if state.results[f"{label}_upload"] is not None:
            state.results[f"{label}_upload"] = None
        if state.role == "client":
            safe_send(control_channel, UPLOAD_RECEIVED)  # vou enviar mesmo que tenha dado errado pra que o teste continue
        else:
            safe_send(control_channel, UPLOAD_ERROR)


async def send_end_test(control_channel, timeout, test_size):
    label = THROUGHPUT_LABELS[test_size]
    response = await events_timeout({"upload_received": state.events["upload_received"],
                                     "upload_error": state.events["upload_error"]
                                     }, timeout)
    if response != "upload_received":
        logger.warning("%s: fim do teste sem confirmação de upload (%s).", label, response)
    if response == "upload_error" and state.results[f"{label}_upload"] is not None:
        state.results[f"{label}_upload"] = None
    safe_send(control_channel, END_TEST)  # somente aqui eu envio o fim do teste, quando da certo ou quando da errado



# alguma das mensagens (tem alguma) de fim que estou mandando, eu nao estou mandando assim que termina o teste
# de upload. Estou mandando quando termina todos os testes de vazão