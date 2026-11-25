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
    PACING_LOW_WATER,
    REPLY_TIMEOUT_SECONDS,
    STALL_WINDOW_SECONDS,
    STALL_FLOOR_BytePerSec)
import logging
from state import state
import json
from utils import event_timeout, events_timeout, safe_send
from storage import save_to_file
import asyncio
import time

logger = logging.getLogger(__name__)

# stall detector clock (replaceable in tests)
_clock = time.monotonic


# latest RTT (ms) from the loaded-latency probes and the number of samples
def _ultimo_rtt_ms(PEER):
    t0 = PEER[state.t0_latency_key()]
    t1 = PEER[state.t1_latency_key()]
    qtd = min(len(t0), len(t1))
    for i in range(qtd - 1, -1, -1):
        if t1[i] is not None:  # None = lost probe
            return (t1[i] - t0[i]) / 10 ** 6, qtd
    return None, qtd


# paces the send based on the measured RTT instead of waiting for the link to saturate
class _DelayPacer:
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
            return  # acts only once per batch
        rtt, amostras = _ultimo_rtt_ms(self.PEER)
        if rtt is not None and amostras > self.amostras_vistas:
            self.amostras_vistas = amostras
            self.pior_rtt = rtt if self.pior_rtt is None else max(self.pior_rtt, rtt)
            if rtt > LOADED_LATENCY_TARGET_MS:
                self.pausa = min(self.pausa * 2 + PACING_STEP, PACING_MAX_PAUSE)
            elif rtt < LOADED_LATENCY_TARGET_MS * PACING_LOW_WATER:
                self.pausa *= PACING_DECAY
                if self.pausa < PACING_STEP:
                    self.pausa = 0.0
            # between LOW_WATER*target and the target the pause is unchanged (dead zone)
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
        watch = _DrainWatch()
        for i in range(0, qtd_pacotes):
            if not safe_send(throughput_channel, package):
                logger.warning("%s: canal de vazão fechou no pacote %s/%s. Abortando o envio.",
                               label, i, qtd_pacotes)
                return False
            if not await _wait_buffer_drain(throughput_channel, limite, label, i, qtd_pacotes, watch):
                return False
            await pacer.step(i)
        logger.info("%s: %s", label, pacer.resumo())
        # waits for the queue to empty before signaling the end: queued is not delivered
        throughput_channel.bufferedAmountLowThreshold = 0
        if not await _wait_buffer_drain(throughput_channel, 0, label, qtd_pacotes - 1,
                                        qtd_pacotes, watch):
            return False
        safe_send(control_channel, END_THROUGHPUT)
        return True
    except Exception as e:
        logger.exception("Erro no envio dos dados da vazão: %s", e)
        return False


# counts buffer timeouts across calls and measures the delivery rate per window
class _DrainWatch:
    def __init__(self, clock=None):
        self.stalls = 0
        self.clock = clock or _clock
        self.t_ref = None
        self.bytes_ref = None

    # (True, rate) if delivery within the window fell below the floor; otherwise (False, rate)
    def taxa_abaixo_do_piso(self, entregues):
        agora = self.clock()
        if self.t_ref is None:
            self.t_ref, self.bytes_ref = agora, entregues
            return False, None
        decorrido = agora - self.t_ref
        if decorrido < STALL_WINDOW_SECONDS:
            return False, None
        taxa = (entregues - self.bytes_ref) / decorrido
        if taxa < STALL_FLOOR_BytePerSec:
            return True, taxa
        self.t_ref, self.bytes_ref = agora, entregues
        return False, taxa


# holds the send until the buffer drops back below the limit
async def _wait_buffer_drain(throughput_channel, limite, label, pacote, qtd_pacotes, watch):
    while throughput_channel.bufferedAmount > limite:
        entregues = max(0, (pacote + 1) * BYTES_PER_PACKAGE - throughput_channel.bufferedAmount)
        travado, taxa = watch.taxa_abaixo_do_piso(entregues)
        if travado:
            logger.error("%s: %.0f B/s nos últimos %ss, abaixo do piso de %.0f B/s. "
                         "SCTP travado: derrubando a rodada pra reparear.",
                         label, taxa, STALL_WINDOW_SECONDS, STALL_FLOOR_BytePerSec)
            state.abort_round(f"SCTP travado no envio de {label}")
            return False
        state.events["throughput_buffer_drained"].clear()
        if throughput_channel.bufferedAmount <= limite:
            return True  # drained between the check and the clear
        if await event_timeout(state.events["throughput_buffer_drained"], BUFFER_DRAIN_TIMEOUT):
            watch.stalls = 0
            continue
        watch.stalls += 1
        logger.warning("%s: buffer parado em %s bytes há %ss (pacote %s/%s)",
                       label, throughput_channel.bufferedAmount,
                       watch.stalls * BUFFER_DRAIN_TIMEOUT, pacote, qtd_pacotes)
        if watch.stalls >= MAX_BUFFER_STALLS:
            logger.error("%s: buffer não drenou em %ss. SCTP travado: derrubando a rodada pra reparear.",
                         label, MAX_BUFFER_STALLS * BUFFER_DRAIN_TIMEOUT)
            # SEND_ABORTED would be stuck behind the stalled queue: the way out is to abort the round
            state.abort_round(f"SCTP travado no envio de {label}")
            return False
        if throughput_channel.readyState != "open":
            return False
    return True


# ends a failed upload and notifies the peer, without waiting for confirmation
def abort_upload(control_channel, test_size):
    label = THROUGHPUT_LABELS[test_size]
    logger.warning("%s: meu envio falhou. Avisando o par e seguindo sem esperar os %ss.",
                   label, REPLY_TIMEOUT_SECONDS)
    state.results[f"{label}_upload"] = None
    safe_send(control_channel, SEND_ABORTED)


# download throughput in Mbps, or None when there is no interval to measure
def _vazao_download_mbps(PEER, label):
    pacotes = PEER["qtd_packages"]
    t0 = PEER["t0_throughput"]
    # t1 comes from the END_THROUGHPUT handler; t0 is the first packet's arrival (hence pacotes - 1)
    t1 = PEER["t1_throughput"] or time.monotonic()
    if pacotes < 2 or t0 is None or t1 <= t0:
        motivo = ("pacotes<2" if pacotes < 2 else
                  "t0 ausente" if t0 is None else "t1<=t0")
        logger.warning("%s: sem intervalo pra medir (%s). Download sem medida. "
                       "[pacotes=%s t0=%s t1=%s delta=%s]",
                       label, motivo, pacotes, t0, PEER["t1_throughput"],
                       None if t0 is None else round(t1 - t0, 6))
        return None
    return round((pacotes - 1) * BYTES_PER_PACKAGE / (t1 - t0) / 10 ** 6, 2) * 8


# waits for the download to finish while watching the receive rate: ("recebido"|"abortado"|"travado", rate)
async def _espera_download(PEER, throughput_finished, label):
    eventos = {"recebido": throughput_finished, "abortado": state.events["send_aborted"]}
    recebidos_ref = PEER["qtd_packages"]
    while True:
        resposta = await events_timeout(eventos, STALL_WINDOW_SECONDS)
        if resposta != "timeout":
            return resposta, None
        recebidos = PEER["qtd_packages"]
        taxa = (recebidos - recebidos_ref) * BYTES_PER_PACKAGE / STALL_WINDOW_SECONDS
        if taxa < STALL_FLOOR_BytePerSec:
            return "travado", taxa
        recebidos_ref = recebidos


async def calculate_throughput(role, PEER, throughput_finished):
    total_bytes_esperada = PEER["qtd_total_bytes"]
    label = THROUGHPUT_LABELS[total_bytes_esperada]
    canal = state.server["channels"][CONTROL] if role == "server" else state.client["control_channel"]
    # no fixed timer: the receive rate decides
    response, taxa = await _espera_download(PEER, throughput_finished, label)
    if response == "recebido":
        # loss under load: expected packets minus received ones
        esperados = PEER["qtd_total_bytes"] // BYTES_PER_PACKAGE
        if esperados > 0:
            perdidos = max(0, esperados - PEER["qtd_packages"])
            state.results[f"{label}_package_loss"] = round(perdidos / esperados * 100, 2)
        vazao_em_Mbps = _vazao_download_mbps(PEER, label)
        state.results[f"{label}_download"] = vazao_em_Mbps
        if role != "server":
            logger.debug("Resultados do cliente: %s", state.results)
        if vazao_em_Mbps is None:
            # the peer gets upload=None instead of an invalid number
            safe_send(canal, UPLOAD_ERROR)
        else:
            safe_send(canal, json.dumps({
                "msg": "upload",
                "value": vazao_em_Mbps,
                "test_size": total_bytes_esperada
            }))
    elif response == "travado":
        # nothing arrived in a whole window: the association is not delivering, so the round is aborted
        logger.error("%s: recebi %.0f B/s nos últimos %ss, abaixo do piso de %.0f B/s. "
                     "Remetente travado: derrubando a rodada pra reparear.",
                     label, taxa, STALL_WINDOW_SECONDS, STALL_FLOOR_BytePerSec)
        state.results[f"{label}_download"] = None
        state.results[f"{label}_package_loss"] = None
        state.abort_round(f"remetente travado no download de {label}")
        return
    else:
        # the peer gave up sending (SEND_ABORTED): the round goes on
        logger.warning("%s: o par desistiu do envio. Download sem medida.", label)
        state.results[f"{label}_download"] = None
        state.results[f"{label}_package_loss"] = None
        safe_send(canal, UPLOAD_ERROR)

    # the server upload does not depend on the download having succeeded
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
        # END_TEST unblocks the client
        safe_send(controle, END_TEST)
        return
    # waits for the upload_received or upload_error event
    await send_end_test(controle, REPLY_TIMEOUT_SECONDS, test_size)


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
            safe_send(control_channel, UPLOAD_RECEIVED)  # sent even on error so the test can continue
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
    safe_send(control_channel, END_TEST)