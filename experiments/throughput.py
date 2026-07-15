from constants import (
    BYTES_PER_PACKAGE, 
    END_THROUGHPUT, 
    MIN_THROUGHPUT_BytePerSec, 
    CONTROL, 
    SHORT_TIMEOUT, 
    BYTES_THROUGHPUT_10MB,
    THROUGHPUT,
    UPLOAD_ERROR,
    UPLOAD_RECEIVED,
    END_TEST,
    THROUGHPUT_LABELS)
import logging
from state import state
import json
from utils import event_timeout, events_timeout
from storage import save_to_file
import asyncio
import time

logger = logging.getLogger(__name__)

async def send_throughput_data(throughput_channel, control_channel, PEER, test_size):
    try:
        package = bytes(BYTES_PER_PACKAGE)
        PEER["qtd_total_bytes"] = test_size
        PEER["qtd_packages"] = 0
        qtd_pacotes = test_size // len(package)
        tam_pacote = len(package)
        logger.debug("o envio dos pacotes vai começar agora. Vou enviar %s pacotes de tamanho %s", qtd_pacotes, tam_pacote)
        
        for i in range(0, qtd_pacotes):
            throughput_channel.send(package)
        control_channel.send(END_THROUGHPUT)
    except Exception as e:
        print(f'Erro no envio dos dados da vazão: {e}')


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
            # state.server["channels"][CONTROL].send(
            #     f'RESULTADO DO TESTE DE client.UPLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
            state.server["channels"][CONTROL].send(json.dumps({
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
            # state.client["control_channel"].send(
            #     f'RESULTADO DO TESTE DE server.UPLOAD: \n A vazão calculada é de {vazao_em_Mbps} Mb/s')
            state.client["control_channel"].send(json.dumps({
                "msg": "upload",
                "value": vazao_em_Mbps,
                "test_size": total_bytes_esperada
            }))
            #logger.info("RESULTADO DO TESTE DE client.DOWNLOAD: \n A vazão calculada é de %s Mb/s para o tamanho de %s Mbytes", vazao_em_Mbps, int(PEER["qtd_total_bytes"])//10**6)
    else:
        # meu download é none e o do outro par é none o upload
        state.results[f"{label}_download"] = None
        if role == "server":
            state.server["channels"][CONTROL].send(UPLOAD_ERROR)
        else:
            state.client["control_channel"].send(UPLOAD_ERROR)

async def calculate_server_upload(test_size):
    await send_throughput_data(state.server["channels"][THROUGHPUT], state.server["channels"][CONTROL], state.server,
                                                     test_size)
            ## a task abaixo irá aguardar o evento upload_received ou upload_error
    await send_end_test(state.server["channels"][CONTROL], test_size / MIN_THROUGHPUT_BytePerSec, test_size)


async def start_server_upload_timeout():
    response = await event_timeout(state.events["start_server_upload"], SHORT_TIMEOUT)
    if response:
        state.server["channels"][CONTROL].send(
                    "Não recebi o ACK do resultado do upload do cliente. Vou iniciar o teste mesmo assim.")
    else:
        state.server["channels"][CONTROL].send("Recebi ACK do upload do cliente. Vou iniciar o teste agora.")


async def send_ack_end_upload(control_channel, timeout, test_size):
    label = THROUGHPUT_LABELS[test_size]
    response = await events_timeout({"upload_received": state.events["upload_received"],
                                     "upload_error": state.events["upload_error"]
                                     }, timeout)
    if response == "upload_received":
        control_channel.send(UPLOAD_RECEIVED)
    else:
        if state.results[f"{label}_upload"] is not None:
            state.results[f"{label}_upload"] = None
        if state.role == "client":
            control_channel.send(UPLOAD_RECEIVED)  # vou enviar mesmo que tenha dado errado pra que o teste continue
        else:
            control_channel.send(UPLOAD_ERROR)


async def send_end_test(control_channel, timeout, test_size):
    label = THROUGHPUT_LABELS[test_size]
    response = await events_timeout({"upload_received": state.events["upload_received"],
                                     "upload_error": state.events["upload_error"]
                                     }, timeout)
    if response == "upload_error" and state.results[f"{label}_upload"] is not None:
        state.results[f"{label}_upload"] = None
    control_channel.send(END_TEST)  # somente aqui eu envio o fim do teste, quando da certo ou quando da errado



# alguma das mensagens (tem alguma) de fim que estou mandando, eu nao estou mandando assim que termina o teste
# de upload. Estou mandando quando termina todos os testes de vazão