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

# relógio do detector de travamento, no módulo pra o teste poder avançar o tempo
# sem dormir (o _DrainWatch nasce dentro de send_throughput_data)
_clock = time.monotonic


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
        # Enfileirar os 71428 pacotes NÃO é entregá-los. Em 2026-09-26 21:18 o laço
        # terminou, a cauda (~1MB) travou no SCTP e o END_THROUGHPUT — enfileirado
        # atrás dela na fila única do aiortc — nunca saiu: servidor e cliente
        # esperando um ao outro por 42min, até o watchdog. O detector de taxa não viu
        # nada porque só roda dentro do laço. Esperar a fila zerar com o MESMO
        # detector faz o sinal sair numa fila vazia e derruba a rodada em uma janela
        # se a cauda travar. É o que torna verdadeiro o "o remetente sempre sinaliza"
        # que justifica as esperas sem cronômetro do outro lado.
        throughput_channel.bufferedAmountLowThreshold = 0
        if not await _wait_buffer_drain(throughput_channel, 0, label, qtd_pacotes - 1,
                                        qtd_pacotes, watch):
            return False
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

    O contador NÃO basta: em 2026-09-26 19:18 o gotejo era de 1 pacote a cada ~10s,
    o evento chegava dentro da janela seguinte de 5s e zerava o contador — 82 avisos
    "há 5s", nunca 6 seguidos. Por isso a decisão de verdade é `taxa_abaixo_do_piso`:
    bytes que avançaram na janela, independente de quando o evento chega.
    """
    def __init__(self, clock=None):
        self.stalls = 0
        self.clock = clock or _clock    # injetável: o teste avança o tempo sem dormir
        self.t_ref = None               # âncora da janela: fixada na 1ª chamada, não aqui
        self.bytes_ref = None

    def taxa_abaixo_do_piso(self, entregues):
        """(True, taxa) se na janela o envio ficou abaixo do piso; senão (False, taxa).

        `entregues` são os bytes que SAÍRAM da fila da aplicação (enfileirados menos
        bufferedAmount) — não os enfileirados. É o que faz o mesmo detector servir
        nas duas fases: no laço (enfileirado cresce, fila cheia) e na cauda (nada mais
        é enfileirado, só a fila baixando). Gotejo é "entregues" parado, em qualquer uma.

        Antes da janela fechar não opina — um teste de 100KB acaba em <1s e nunca
        chega a decidir. Uma janela boa reancora, então o gotejo é medido sempre sobre
        os últimos STALL_WINDOW_SECONDS, não desde o início do envio.
        """
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


async def _wait_buffer_drain(throughput_channel, limite, label, pacote, qtd_pacotes, watch):
    """Segura o envio até o buffer voltar abaixo do limite.

    O código antigo esperava UMA vez e seguia enfileirando mesmo sem drenar. Com
    100MB isso enche a fila do gargalo em segundos: o RTT passa dos 0,5s que o
    consent freshness do ICE (RFC 7675) tolera, o aioice acumula 6 falhas e fecha
    a conexão ~30s depois. Aqui a espera é um laço de verdade.
    """
    while throughput_channel.bufferedAmount > limite:
        # TAXA, não "drenou ou não": 1 pacote a cada 10s dispara o evento e zera o
        # contador abaixo. Só a janela com piso pega um gotejo, seja qual for o ritmo.
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
            return True  # drenou entre a checagem e o clear
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
            # Não adianta só abortar o envio e avisar o par: o aiortc tem UMA fila de
            # saída pra todos os datachannels, e o SEND_ABORTED entraria atrás dos
            # ~1MB de vazão travada — nunca sairia. Foi o deadlock de 2026-09-26:
            # cliente e servidor esperando um ao outro por 45min. Um SCTP que não
            # escoa 1400 bytes em 30s é uma conexão morta; a saída é reparear com
            # uma associação nova, e é isso que connection_lost faz o main() fazer.
            state.abort_round(f"SCTP travado no envio de {label}")
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
                   label, REPLY_TIMEOUT_SECONDS)
    state.results[f"{label}_upload"] = None
    safe_send(control_channel, SEND_ABORTED)


def _vazao_download_mbps(PEER, label):
    """Vazão do download em Mbps, ou None quando não há intervalo pra medir.

    O `-1` existe porque t0 é carimbado na chegada do PRIMEIRO pacote: o intervalo
    cobre qtd_packages-1 pacotes. Com ZERO pacotes recebidos o numerador virava -1400
    e a vazão saía NEGATIVA — `-22.8` e `-0.72 Mbps` no results.csv de 2026-10-04 — e
    um número inválido atravessa qualquer média sem avisar, o que é pior que célula
    vazia. Com 1 pacote dava 0.0, igualmente falso. E t0 ausente (nenhum pacote neste
    teste, campo zerado no primeiro pacote do teste) levantava TypeError.
    """
    pacotes = PEER["qtd_packages"]
    t0 = PEER["t0_throughput"]
    # t1 é carimbado no handler do END_THROUGHPUT, na CHEGADA. Aqui pode ser muito
    # depois: o cliente consome o evento só depois das 20 sondas de latência, que sob
    # carga levam até 80s. Em 2026-09-26 isso deu 10MB_download de 0,48 Mbps para um
    # download real de ~7 — o "tempo" incluía 40s de nada.
    # monotônico aqui também, OBRIGATORIAMENTE: misturar com time.time() daria t0≈1e5
    # contra t1≈1.8e9, ou seja "1,8 bilhão de segundos" de intervalo e vazão ~0,00 Mbps
    # — erro silencioso, pior que o negativo que isto veio corrigir.
    t1 = PEER["t1_throughput"] or time.monotonic()
    if pacotes < 2 or t0 is None or t1 <= t0:
        # Os VALORES no aviso, não só o veredito: em 2026-10-04 este aviso saiu com
        # 7142 pacotes (o 10MB inteiro) e não houve como saber se foi t0=None ou
        # t1<=t0 — eu chutei "salto de relógio" e estava errado. Com os números a
        # próxima ocorrência se decide lendo a linha.
        motivo = ("pacotes<2" if pacotes < 2 else
                  "t0 ausente" if t0 is None else "t1<=t0")
        logger.warning("%s: sem intervalo pra medir (%s). Download sem medida. "
                       "[pacotes=%s t0=%s t1=%s delta=%s]",
                       label, motivo, pacotes, t0, PEER["t1_throughput"],
                       None if t0 is None else round(t1 - t0, 6))
        return None
    return round((pacotes - 1) * BYTES_PER_PACKAGE / (t1 - t0) / 10 ** 6, 2) * 8


async def _espera_download(PEER, throughput_finished, label):
    """Espera o fim do download vigiando a taxa de RECEPÇÃO. Espelho do detector de envio.

    Devolve ("recebido"|"abortado"|"travado", taxa_ou_None).

    Esta espera era `timeout=None`, posta em 2026-09-25 com o raciocínio "o remetente
    sempre sinaliza". Em 2026-10-03 o remetente sinalizou — e a entrega falhou: o
    END_THROUGHPUT do servidor não chegou (associação SCTP travada nesse sentido) e o
    cliente ficou 46min preso aqui, as duas máquinas vivas e heartbeatando. O erro foi
    confundir "foi enviado" com "foi recebido".

    Cronômetro fixo também não serve: o download legítimo do 100MB leva minutos e foi
    justamente o que o antigo `test_size / MIN_THROUGHPUT` (800s) errava nos dois
    sentidos. O que decide é o mesmo critério do lado de quem envia — bytes por janela:
    chegando dados, espera o quanto precisar; parado, o remetente travou.
    """
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
    total_bytes_esperada = PEER[
        "qtd_total_bytes"]  ## ex.: teria o BYTES_THROUGHPUT_10MB como o valor dessa chave tam_bytes_test
    label = THROUGHPUT_LABELS[total_bytes_esperada]  
    canal = state.server["channels"][CONTROL] if role == "server" else state.client["control_channel"]
    # Sem cronômetro fixo: quem decide é a taxa de recepção (ver _espera_download).
    # Nada de `test_size / MIN_THROUGHPUT` aqui — em 2026-09-25 esse prazo venceu
    # enquanto o cliente ainda subia, o servidor começou o próprio upload em cima do
    # dele, os dois floods travaram o caminho e o consent expirou dos dois lados.
    response, taxa = await _espera_download(PEER, throughput_finished, label)
    if response == "recebido":
        # Perda SOB CARGA, de graça: o receptor já tem as duas pontas. A coluna
        # `package_loss` mede com o enlace ocioso (~300 kbps) e está certa — o iperf3
        # mediu 0/782 nessa taxa em 2026-10-07, igual à ferramenta. Mas o teste de
        # vazão opera perto da saturação, e lá o mesmo iperf mediu 6,5% a 18%. É este
        # regime que explica o travamento do 100MB, e agora ele fica medido por tamanho.
        esperados = PEER["qtd_total_bytes"] // BYTES_PER_PACKAGE
        if esperados > 0:
            perdidos = max(0, esperados - PEER["qtd_packages"])   # nunca negativa
            state.results[f"{label}_package_loss"] = round(perdidos / esperados * 100, 2)
        vazao_em_Mbps = _vazao_download_mbps(PEER, label)
        state.results[f"{label}_download"] = vazao_em_Mbps  # It's here when the tests finish
        if role != "server":
            logger.info("Resultados do cliente: %s", state.results)
        if vazao_em_Mbps is None:
            # o par fica com upload=None em vez de receber um número inválido
            safe_send(canal, UPLOAD_ERROR)
        else:
            safe_send(canal, json.dumps({
                "msg": "upload",
                "value": vazao_em_Mbps,
                "test_size": total_bytes_esperada
            }))
    elif response == "travado":
        # Só este caso derruba a rodada: nada chegou numa janela inteira, o remetente
        # está vivo mas a associação não entrega. Não dá pra seguir pro upload do
        # servidor num caminho morto — seria enfileirar dados que ninguém recebe.
        logger.error("%s: recebi %.0f B/s nos últimos %ss, abaixo do piso de %.0f B/s. "
                     "Remetente travado: derrubando a rodada pra reparear.",
                     label, taxa, STALL_WINDOW_SECONDS, STALL_FLOOR_BytePerSec)
        state.results[f"{label}_download"] = None
        state.results[f"{label}_package_loss"] = None   # sem download, sem perda medida
        state.abort_round(f"remetente travado no download de {label}")
        return
    else:
        # o par desistiu de enviar (SEND_ABORTED): sem medida, mas a conexão está boa
        # e o upload DELE ainda vem — a rodada continua
        logger.warning("%s: o par desistiu do envio. Download sem medida.", label)
        state.results[f"{label}_download"] = None
        state.results[f"{label}_package_loss"] = None   # sem download, sem perda medida
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
    await send_end_test(controle, REPLY_TIMEOUT_SECONDS, test_size)   # resposta, não transferência


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