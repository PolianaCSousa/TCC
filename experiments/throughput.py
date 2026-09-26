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
    SEND_ABORTED,
    END_TEST,
    THROUGHPUT_LABELS,
    LOADED_LATENCY_TARGET_MS,
    PACING_BATCH,
    PACING_STEP,
    PACING_DECAY,
    PACING_MAX_PAUSE,
    PACING_LOW_WATER)
import logging
from state import state
import json
from utils import event_timeout, events_timeout, safe_send
from storage import save_to_file
import asyncio
import time

logger = logging.getLogger(__name__)


def _ultimo_rtt_ms(PEER):
    """Última medida de ida-e-volta das sondas de latência sob carga, em ms.

    Devolve (rtt, qtd_amostras). O contador importa tanto quanto o valor: sem ele o
    controlador continuaria reagindo à MESMA amostra depois que as sondas acabam
    (são só LATENCY_TEST_SIZE=20 por fase) e escorregaria numa direção só.
    """
    t0 = PEER[state.t0_latency_key()]
    t1 = PEER[state.t1_latency_key()]
    qtd = min(len(t0), len(t1))
    for i in range(qtd - 1, -1, -1):
        if t1[i] is not None:            # None = sonda que se perdeu, não serve de sinal
            return (t1[i] - t0[i]) / 10 ** 6, qtd
    return None, qtd


class _DelayPacer:
    """Espaça o envio em função do RTT, em vez de esperar o link transbordar.

    O congestion control do SCTP é baseado em perda, e perda só aparece quando a
    fila do gargalo estoura — ele enche a fila por projeto. É o que levou a
    loaded_latency a 1451ms e travou a associação em 2026-09-23.

    A lei é multiplicativa nos dois sentidos (x2 pra subir, x0,5 pra descer), porque
    o orçamento de realimentação é pequeno: ~20 sondas por fase. Recuperação aditiva
    deixaria a pausa presa no teto depois de um pico no começo do envio.

    Limitação conhecida: as sondas só rodam durante o upload do CLIENTE, então é só
    lá que existe sinal fresco. No upload do servidor o controlador segura a pausa
    em que estiver (zero, na prática). Medir isso exige sondas naquela fase também.
    """

    def __init__(self, PEER, label):
        self.PEER = PEER
        self.label = label
        self.pausa = 0.0
        self.amostras_vistas = 0
        self.ajustes = 0
        self.pausa_total = 0.0
        self.pior_rtt = None

    async def step(self, pacote):
        if pacote % PACING_BATCH:
            return                        # só age de lote em lote
        rtt, amostras = _ultimo_rtt_ms(self.PEER)
        if rtt is not None and amostras > self.amostras_vistas:
            self.amostras_vistas = amostras
            self.pior_rtt = rtt if self.pior_rtt is None else max(self.pior_rtt, rtt)
            if rtt > LOADED_LATENCY_TARGET_MS:
                self.pausa = min(self.pausa * 2 + PACING_STEP, PACING_MAX_PAUSE)
            elif rtt < LOADED_LATENCY_TARGET_MS * PACING_LOW_WATER:
                self.pausa *= PACING_DECAY
                if self.pausa < PACING_STEP:
                    self.pausa = 0.0     # decaimento puro nunca zera; aqui ele solta de vez
            # entre LOW_WATER*alvo e o alvo fica a zona morta: sem ela o controlador
            # oscila em volta do alvo e o envio vira serrote
            self.ajustes += 1
        if self.pausa:
            self.pausa_total += self.pausa
            await asyncio.sleep(self.pausa)

    def resumo(self):
        pior = f"{self.pior_rtt:.0f}ms" if self.pior_rtt is not None else "sem amostra"
        return (f"autoajuste: pausa final {self.pausa*1000:.1f}ms, "
                f"{self.pausa_total:.1f}s segurando no total, "
                f"{self.ajustes} ajuste(s), pior RTT {pior} (alvo {LOADED_LATENCY_TARGET_MS}ms)")


async def send_throughput_data(throughput_channel, control_channel, PEER, test_size):
    label = THROUGHPUT_LABELS[test_size]
    try:
        package = bytes(BYTES_PER_PACKAGE)
        PEER["qtd_total_bytes"] = test_size
        PEER["qtd_packages"] = 0
        qtd_pacotes = test_size // len(package)
        limite = BUFFER_AMOUNT_LIMIT[test_size]
        logger.info("%s: enviando %s pacotes de %s bytes", label, qtd_pacotes, len(package))

        pacer = _DelayPacer(PEER, label)
        watch = _DrainWatch()                    # um por envio: o contador atravessa o laço
        for i in range(0, qtd_pacotes):
            if not safe_send(throughput_channel, package):
                logger.warning("%s: canal de vazão fechou no pacote %s/%s. Abortando o envio.",
                               label, i, qtd_pacotes)
                return False
            if not await _wait_buffer_drain(throughput_channel, limite, label, i, qtd_pacotes, watch):
                return False
            await pacer.step(i)
        logger.info("%s: %s", label, pacer.resumo())
        safe_send(control_channel, END_THROUGHPUT)
        return True
    except Exception as e:
        logger.exception("Erro no envio dos dados da vazão: %s", e)
        return False


class _DrainWatch:
    """Contador de timeouts do buffer que SOBREVIVE entre chamadas de _wait_buffer_drain.

    Antes o contador nascia zerado a cada chamada, e o aborto exigia os 6 timeouts
    dentro de UMA chamada. Em 2026-09-26 o link caiu pra ~200 B/s: a cada ~7s escoava
    1 pacote, o buffer cruzava o limite, a função retornava True, o laço mandava 1
    pacote e chamava de novo — contador zerado. Ficou 22 min logando "há 5s", nunca
    "há 10s", avançando 1 pacote por aviso, até ser morto na mão. Um link gotejando
    é indistinguível de um link morto pra medição, e tem que abortar como um.

    Aqui só uma drenagem DE VERDADE — o evento bufferedamountlow chegando dentro do
    prazo — zera o contador. Timeouts acumulam entre chamadas.
    """
    def __init__(self):
        self.stalls = 0


async def _wait_buffer_drain(throughput_channel, limite, label, pacote, qtd_pacotes, watch):
    """Segura o envio até o buffer voltar abaixo do limite.

    O código antigo esperava UMA vez e seguia enfileirando mesmo sem drenar. Com
    100MB isso enche a fila do gargalo em segundos: o RTT passa dos 0,5s que o
    consent freshness do ICE (RFC 7675) tolera, o aioice acumula 6 falhas e fecha
    a conexão ~30s depois. Aqui a espera é um laço de verdade.
    """
    while throughput_channel.bufferedAmount > limite:
        state.events["throughput_buffer_drained"].clear()
        if throughput_channel.bufferedAmount <= limite:
            return True  # drenou entre a checagem e o clear
        if await event_timeout(state.events["throughput_buffer_drained"], BUFFER_DRAIN_TIMEOUT):
            watch.stalls = 0
            continue
        watch.stalls += 1
        logger.warning("%s: buffer parado em %s bytes há %ss (pacote %s/%s)",
                       label, throughput_channel.bufferedAmount,
                       watch.stalls * BUFFER_DRAIN_TIMEOUT, pacote, qtd_pacotes)
        if watch.stalls >= MAX_BUFFER_STALLS:
            logger.error("%s: buffer não drenou em %ss. Abortando o envio pra não derrubar a conexão.",
                         label, MAX_BUFFER_STALLS * BUFFER_DRAIN_TIMEOUT)
            return False
        if throughput_channel.readyState != "open":
            return False
    return True


def abort_upload(control_channel, test_size):
    """Encerra um upload que falhou, sem esperar a confirmação que não vem.

    O `send_throughput_data` já devolvia False quando desistia, mas os chamadores
    ignoravam o retorno: o remetente ficava os `test_size / MIN_THROUGHPUT` segundos
    inteiros (800s no 100MB) esperando o resultado do par, e o par ficava o mesmo
    tanto esperando um END_THROUGHPUT que nunca tinha sido enviado. Em 2026-09-23 as
    duas esperas juntas transformaram um envio travado aos 34% numa rodada de 40min.
    """
    label = THROUGHPUT_LABELS[test_size]
    logger.warning("%s: meu envio falhou. Avisando o par e seguindo sem esperar os %ss.",
                   label, test_size / MIN_THROUGHPUT_BytePerSec)
    state.results[f"{label}_upload"] = None
    safe_send(control_channel, SEND_ABORTED)


async def calculate_throughput(role, PEER, throughput_finished):
    total_bytes_esperada = PEER[
        "qtd_total_bytes"]  ## ex.: teria o BYTES_THROUGHPUT_10MB como o valor dessa chave tam_bytes_test
    label = THROUGHPUT_LABELS[total_bytes_esperada]  
    canal = state.server["channels"][CONTROL] if role == "server" else state.client["control_channel"]
    # SEM timeout, de propósito. O remetente agora SEMPRE sinaliza o fim — END_THROUGHPUT
    # se completou, SEND_ABORTED se desistiu (abort_upload) — então esperar um cronômetro
    # em vez do sinal só pode dar errado: em 2026-09-25 o upload do cliente, freado pelo
    # autoajuste, passou dos 800s; o servidor desistiu de esperar e começou o PRÓPRIO
    # upload de 100MB em cima do que ainda estava chegando. Dois floods no mesmo caminho,
    # STUN não atravessou em nenhuma direção, e o consent expirou dos dois lados 18min
    # depois. Se o remetente sumir sem sinalizar, a conexão cai e cancel_round_tasks()
    # cancela esta espera; se ficar viva e mudo, o ROUND_WATCHDOG encerra a rodada.
    response = await events_timeout({"recebido": throughput_finished,
                                     "abortado": state.events["send_aborted"]}, timeout=None)
    if response == "recebido":
        PEER["t1_throughput"] = time.time()
        tempo = PEER["t1_throughput"] - PEER["t0_throughput"]
        vazao_em_bytes = ((PEER["qtd_packages"] - 1) * BYTES_PER_PACKAGE) / tempo  # 1400 é o tamanho do pacote
        vazao_em_MB = round(vazao_em_bytes / 10 ** 6, 2)
        vazao_em_Mbps = vazao_em_MB * 8
        state.results[f"{label}_download"] = vazao_em_Mbps  # It's here when the tests finish
        if role != "server":
            logger.info("Resultados do cliente: %s", state.results)
        safe_send(canal, json.dumps({
            "msg": "upload",
            "value": vazao_em_Mbps,
            "test_size": total_bytes_esperada
        }))
    else:
        # meu download é none e o do outro par é none o upload
        logger.warning("%s: o par desistiu do envio. Download sem medida.", label)
        state.results[f"{label}_download"] = None
        safe_send(canal, UPLOAD_ERROR)

    # a subida do servidor NÃO depende da descida ter dado certo: são direções
    # independentes. Antes isto ficava dentro do `if`, então uma falha na descida
    # cancelava a subida — foi por isso que o 100MB_upload do servidor ficou vazio em
    # 2026-09-23 e o cliente ainda esperou 1600s por dados que nunca viriam.
    if role == "server":
        await start_server_upload_timeout()
        await calculate_server_upload(state.server["qtd_total_bytes"])

async def calculate_server_upload(test_size):
    controle = state.server["channels"][CONTROL]
    state.server["channels"][THROUGHPUT].bufferedAmountLowThreshold = BUFFER_AMOUNT_LIMIT[test_size]
    enviou = await send_throughput_data(state.server["channels"][THROUGHPUT], controle, state.server,
                                                     test_size)
    if not enviou:
        abort_upload(controle, test_size)
        # o END_TEST é o que destrava o cliente: sem ele, ele espera mais 800s
        safe_send(controle, END_TEST)
        return
            ## a task abaixo irá aguardar o evento upload_received ou upload_error
    await send_end_test(controle, test_size / MIN_THROUGHPUT_BytePerSec, test_size)


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