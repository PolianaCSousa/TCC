# TCC — Benchmark de rede P2P

Ferramenta que mede a qualidade da conexão entre dois computadores quaisquer na
internet, sem servidor no meio. Dois peers se encontram sozinhos, abrem uma
conexão WebRTC direta e medem latência, latência sob carga, vazão e perda de
pacotes. Os resultados vão para um CSV e para um banco de séries temporais, e
você os visualiza no Grafana.

> **Status:** a stack em Docker ainda não foi executada de ponta a ponta. Veja
> [Pendências conhecidas](#pendências-conhecidas) antes de abrir uma issue.

---

## Como funciona

Sobem quatro containers:

| Container | Papel |
|---|---|
| `ipfs` | rendezvous — é por onde os peers se descobrem e trocam a negociação da conexão |
| `peer` | o benchmark em si: acha um par, conecta e roda os testes |
| `influxdb` | guarda as medições como série temporal |
| `grafana` | mostra os gráficos |

O ciclo de uma rodada:

1. O peer anuncia sua presença a cada 2 segundos num tópico público do IPFS
   (`tcc-polics`), informando seu status e o **ASN** — o número que identifica o
   provedor de internet dele.
2. Ao ver outro peer livre, escolhe um parceiro. A preferência é por alguém de
   **ASN diferente**, ou seja, de outro provedor; só cai para o mesmo ASN se não
   houver outra opção. É o que torna a medição interessante: mede-se o caminho
   entre operadoras, não dentro da mesma.
3. Os dois combinam quem é cliente e quem é servidor, trocam oferta e resposta
   WebRTC pelo próprio IPFS, e abrem a conexão direta.
4. Sobem quatro canais de dados — controle, latência, vazão e perda de pacotes —
   e os testes rodam em sequência, incluindo latência medida com o link saturado.
5. O resultado é gravado no `results.csv` e no InfluxDB.
6. O peer espera 30 segundos, volta a ficar livre e procura um novo par.

**Você precisa de pelo menos duas máquinas rodando a ferramenta.** Um peer
sozinho fica anunciando indefinidamente sem nada para medir — não é erro, é falta
de par.

---

## Pré-requisitos

- **Docker.** No Windows e no Mac, instale o [Docker
  Desktop](https://www.docker.com/products/docker-desktop/) e deixe-o aberto. No
  Linux, o Docker Engine com o plugin do Compose.
- **Git**, para clonar o repositório.

Confira se está tudo certo:

```bash
docker compose version
```

---

## Configuração

### 1. Clone o repositório

```bash
git clone -b docker https://github.com/PolianaCSousa/TCC.git
cd TCC
```

### 2. Crie o seu `.env`

```bash
cp .env.example .env
```

Abra o `.env` e preencha. Os valores são seus e ficam só na sua máquina — o
arquivo não vai para o git.

| Variável | O que colocar |
|---|---|
| `INFLUXDB_PASSWORD` | senha de acesso ao banco, **mínimo 8 caracteres** |
| `INFLUXDB_TOKEN` | uma chave aleatória; gere com `openssl rand -hex 32` |
| `GRAFANA_PASSWORD` | a senha com que você vai entrar no Grafana |
| `TURN_API_KEY` | opcional — veja abaixo |

As demais já vêm com valores prontos e você não precisa mexer.

**Sobre o `TURN_API_KEY`:** é a chave de um servidor de relay, usada quando os
dois peers estão atrás de NAT restritivo e não conseguem se conectar direto. Sem
ela a ferramenta funciona normalmente, usando apenas o STUN público do Google —
só algumas combinações de rede vão falhar em conectar. Para obter uma, crie uma
conta gratuita em [metered.ca](https://www.metered.ca/).

**No Linux**, ajuste também o `APP_UID` e o `APP_GID` para os seus, senão os
arquivos gerados saem pertencendo ao root:

```bash
id -u    # vai em APP_UID
id -g    # vai em APP_GID
```

No Mac e no Windows pode ignorar — o Docker Desktop resolve a permissão sozinho.

---

## Subindo

```bash
docker compose up -d --build
```

A primeira vez demora: o Docker baixa as imagens e constrói a do peer. O `-d`
deixa tudo rodando em segundo plano.

Acompanhe:

```bash
docker compose ps          # todos devem estar "running", o influx "healthy"
docker compose logs -f peer
```

Nos primeiros minutos é normal ver o peer só anunciando. Ele depende de encontrar
outro peer na rede pública do IPFS, e essa descoberta leva algum tempo.

---

## Vendo os resultados

**Grafana** — <http://localhost:3000>

Usuário `admin`, senha a que você pôs em `GRAFANA_PASSWORD`. A conexão com o
banco já vem configurada.

**InfluxDB** — <http://localhost:8086>

Usuário e senha de `INFLUXDB_USERNAME` e `INFLUXDB_PASSWORD`. Útil para
inspecionar os dados crus.

**CSV** — `data/peer1/results.csv`

Uma linha por rodada, com todas as métricas. Os logs ficam em `data/peer1/logs/`.
Este arquivo é a fonte principal: se a escrita no banco falhar, o teste continua e
o CSV é gravado do mesmo jeito.

---

## Onde os dados ficam

| O quê | Onde | Sobrevive a `docker compose down`? |
|---|---|---|
| `results.csv` e logs | `data/peer1/` — pasta do projeto | sim, é uma pasta sua |
| Banco do Influx | volume `influx-data` | sim |
| Dashboards do Grafana | volume `grafana-data` | sim |

⚠️ **`docker compose down -v` apaga os volumes.** A diferença para o `down`
comum é uma letra, e o banco vai junto. A pasta `data/` não é afetada.

---

## Comandos do dia a dia

```bash
docker compose ps                  # o que está no ar
docker compose logs -f peer        # acompanhar o benchmark
docker compose restart peer        # reiniciar só o peer
docker compose down                # desligar tudo, preservando os dados
docker compose up -d --build       # reconstruir após mudar o código
```

---

## Desenvolvimento

A imagem do peer é construída a partir do **branch `docker` no GitHub**, não do
seu disco:

```yaml
build:
  context: https://github.com/PolianaCSousa/TCC.git#docker
```

Ou seja: alterou um `.py`, precisa commitar e dar push antes que o
`docker compose build` enxergue a mudança. Para iterar rápido durante o
desenvolvimento, troque aquela linha por `context: .`, que constrói do disco.

Já os arquivos montados por bind — `docker/ipfs/` e `grafana/` — não passam pelo
build. Vêm direto do seu clone, e um `docker compose restart` já os aplica.

### Ajustando o Grafana

Montou um painel bom na interface? Exporte como JSON e salve em
`grafana/dashboards/`. Quem clonar o repositório vai abrir o Grafana com o painel
já pronto, sem precisar montar nada.

---

## Pendências conhecidas

- **Não existe dashboard ainda.** A pasta `grafana/dashboards/` está vazia, então
  hoje o Grafana abre sem nenhum painel. Os dados chegam ao banco, mas para vê-los
  é preciso ir em *Explore* e escrever a consulta na mão.
- **O healthcheck do Influx não foi verificado.** Ele usa `influx ping`, e não
  está confirmado se esse CLI vem dentro da imagem 2.x. Se não vier, o
  healthcheck nunca passa e o peer fica travado esperando. Para checar:
  `docker run --rm --entrypoint sh influxdb:2.9.1 -c 'command -v influx curl wget'`
- **As medições atravessam a rede do Docker.** O peer usa a rede padrão do
  Compose para funcionar igual nos três sistemas operacionais. O efeito sobre
  latência e vazão é desprezível, mas a camada extra de NAT pode influenciar o
  `candidate_type` — o que deve constar na metodologia.
- **Um peer por máquina.** A pasta `data/peer1` está fixa no compose; duas
  instâncias na mesma máquina escreveriam por cima uma da outra.
- **Sem serviço de backup.** O `results.csv` existe em um único lugar, sem cópia.
  Copie a pasta `data/` para algum lugar seguro.
