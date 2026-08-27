# TCC — Benchmark de rede P2P via WebRTC com sinalização IPFS

Dois computadores em redes diferentes se encontram por **pubsub do IPFS**, negociam uma conexão **WebRTC** direta e medem latência, latência carregada, vazão e perda de pacotes entre si. Os resultados saem em `results.csv` na máquina de cada um.

Este guia é para **Linux**. Testado em Ubuntu com Python 3.12 e Kubo 0.42.0.

---

## 1. Dependências do sistema

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git wget
```

Versões mínimas: **Python 3.10+** (recomendado 3.12).

> **Só se o `pip install` falhar compilando o `aiortc`:** normalmente ele instala por wheel pré-compilado e nada abaixo é necessário. Se der erro de compilação, instale as libs de mídia e tente de novo:
> ```bash
> sudo apt install -y build-essential python3-dev pkg-config \
>   libavdevice-dev libavfilter-dev libopus-dev libvpx-dev libsrtp2-dev
> ```

## 2. Instalar o IPFS (Kubo)

```bash
wget https://dist.ipfs.tech/kubo/v0.42.0/kubo_v0.42.0_linux-amd64.tar.gz
tar -xvzf kubo_v0.42.0_linux-amd64.tar.gz
cd kubo
sudo bash install.sh
ipfs --version    # deve mostrar: ipfs version 0.42.0
cd ..
```

## 3. Instalar o projeto

```bash
git clone <URL-DO-REPO> TCC
cd TCC
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

O `venv/` **não** vem no repositório — cada máquina cria o seu.

## 4. Criar o arquivo `.env`

O `.env` é ignorado pelo git, então precisa ser criado à mão na raiz do projeto:

```bash
TURN_API_KEY=<peça a chave para a Poliana>
```

Só isso é obrigatório. As variáveis de InfluxDB não são necessárias — a escrita no banco está desativada ([storage.py](storage.py)), os resultados vão só para o CSV.

Sem a `TURN_API_KEY` o programa ainda roda, mas cai só no STUN do Google: se as duas pontas estiverem atrás de NAT restritivo, a conexão WebRTC pode falhar.

---

## 5. Configurar o nó IPFS (uma vez só)

```bash
ipfs init
ipfs config --json Pubsub.Enabled true
```

Se a máquina já tinha um `ipfs init` feito antes, rode só a linha do `Pubsub`.

## 6. Subir o daemon IPFS

**Terminal 1** — deixe rodando durante todo o experimento:

```bash
ipfs daemon
```

Espere aparecer `Daemon is ready`.

## 7. Conectar os dois nós (opcional — atalho)

**Normalmente não é preciso fazer nada aqui.** Quando o `peer.py` assina o tópico, o Kubo anuncia na DHT pública que aquele nó participa de `tcc-polics` e procura os outros que anunciaram o mesmo. A descoberta é automática, mas pode levar de alguns segundos a poucos minutos.

Se depois de uns 2 minutos os dois ainda não se enxergarem (veja o passo 9), force a conexão. Cada um pega o próprio PeerID:

```bash
ipfs id -f='<id>\n'
```

Troquem os IDs (WhatsApp, e-mail, tanto faz). Aí **um dos dois** conecta no outro:

```bash
ipfs swarm connect /p2p/<PEER_ID_DO_OUTRO>
```

Deve responder `connect <PEER_ID> success`. A conexão é bidirecional — basta um lado fazer.

Se falhar, veja [Problemas comuns](#problemas-comuns) no fim.

## 8. Rodar a aplicação

**Terminal 3**, com o daemon já no ar:

```bash
cd ~/TCC          # ajuste para onde você clonou
source venv/bin/activate
python3 peer.py
```

Os dois lados precisam estar rodando `peer.py` ao mesmo tempo. Quem faz papel de cliente e quem faz de servidor é decidido automaticamente pelo pareamento.

O programa roda em **loop**: termina uma rodada de testes, espera 30 segundos e pareia de novo. Encerre com `Ctrl+C` quando quiser.

---

## 9. Conferir se os pares se acharam

No **Terminal 2**, enquanto tudo roda:

```bash

# ou acompanhe as mensagens ao vivo (announce, pair_request, pair_accept)
ipfs pubsub sub tcc-polics

# lista os PeerIDs dos outros nós inscritos no tópico
ipfs pubsub peers tcc-polics

```

Se `pubsub peers` vier vazio, na ordem: confirme que o `peer.py` do **outro lado** está rodando (quem assina o tópico é a aplicação, não o daemon sozinho); espere até ~2 minutos pela descoberta automática na DHT; e só então force com o `swarm connect` do passo 7.

No terminal do `peer.py`, o sinal de que deu certo é a sequência:

```
oferta criada no par cliente
answer do par servidor recebida no cliente
Connection state: connected
Candidate local: <IP> (host|srflx|relay)
```

## 10. Onde ficam os resultados

| Arquivo | Conteúdo |
|---|---|
| `results.csv` | uma linha por rodada, com latência (ms), jitter (ms), vazão (Mbps) e perda de pacotes (%) |
| `logs/peer_5001.log` | log completo daquele peer (o número é a porta da API do Kubo) |

Ambos são gerados localmente e estão no `.gitignore`. Ao final do experimento, **mande o seu `results.csv` para a Poliana**.

---

## Problemas comuns

**`ModuleNotFoundError: No module named 'aiortc'`**
Você rodou com o Python do sistema. Ative o venv antes: `source venv/bin/activate`.

**`Error: this action must be run in online mode` ou `connection refused` na porta 5001**
O daemon não está no ar. Volte ao passo 6.

**`ipfs swarm connect` falha com `no good addresses` / `context deadline exceeded`**
O DHT ainda não achou o endereço do outro nó. Alternativas, em ordem:
1. Espere ~1 minuto depois de subir o daemon e tente de novo.
2. O outro lado roda `ipfs id` e manda o multiaddr público completo (uma linha do campo `Addresses` que comece com um IP público, não `127.0.0.1` nem `192.168.x.x`), e você conecta nele diretamente:
   ```bash
   ipfs swarm connect /ip4/<IP_PUBLICO>/tcp/4001/p2p/<PEER_ID>
   ```
3. Se ambos estiverem atrás de NAT fechado, libere/encaminhe a porta **4001 (TCP e UDP)** no roteador de pelo menos um dos dois.

**Os peers se acham no pubsub mas o WebRTC fica em `failed`**
ICE não conseguiu nenhum caminho. Confirme que a `TURN_API_KEY` está no `.env` — sem TURN, NATs simétricos não fecham conexão.

**Os testes nunca começam, só ficam anunciando**
O pareamento prefere parceiros de **ASN diferente** ([ipfs_signaling.py](ipfs_signaling.py)), que é justamente o caso de vocês em provedores diferentes. Se mesmo assim não pareia, confirme com `ipfs pubsub peers tcc-polics` que os dois estão no tópico.

**A descoberta automática demora ou não acontece**
O Kubo faz a descoberta de peers do tópico pela DHT (`TopicDiscovery`), com *backoff* nas retentativas — a primeira busca é rápida, mas se ela falhar, a próxima só vem depois de cerca de 1 minuto. Ou seja: dar um tempo resolve na maioria dos casos. Se não resolver, o `swarm connect` do passo 7 elimina a dependência da DHT.

---

## Apêndice — vários nós na mesma máquina (só para testes locais)

Cada nó precisa de um repositório e de portas próprias. Setup **uma vez** por nó:

```bash
export IPFS_PATH="$HOME/.ipfs2"
ipfs init
ipfs config Addresses.API /ip4/127.0.0.1/tcp/5002
ipfs config Addresses.Gateway /ip4/127.0.0.1/tcp/8081
ipfs config --json Addresses.Swarm '["/ip4/0.0.0.0/tcp/4002","/ip4/0.0.0.0/udp/4002/quic-v1"]'
ipfs config --json Pubsub.Enabled true
```

Depois, a cada uso, um terminal por daemon:

```bash
export IPFS_PATH="$HOME/.ipfs2" && ipfs daemon
```

E um terminal por peer, apontando para a API correspondente:

```bash
source venv/bin/activate
KUBO_API=http://127.0.0.1:5002 python3 peer.py
```

Na mesma máquina o `swarm connect` não é necessário — os nós se descobrem por mDNS. Atenção: todos terão o mesmo ASN, então o pareamento cai no fallback de "mesmo ASN"; e com 3 nós, dois pareiam e um fica sobrando. Os `peer.py` rodando do mesmo diretório também escrevem no mesmo `results.csv`.
