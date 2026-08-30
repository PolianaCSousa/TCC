#!/bin/bash

# init em runtime, não no build: cada container gera a própria chave e, portanto,
# o próprio PeerID. O -f garante idempotência se o repo já existir (volume montado
# ou restart do mesmo container), pra não trocar a identidade sem querer.
if [ ! -f "${IPFS_PATH:-/root/.ipfs}/config" ]; then
    ipfs init
    ipfs config --json Pubsub.Enabled true
fi

# Start the background task/service
ipfs daemon &
# eu posso jogar os logs do ipfs so pra um arquivo pra deixar mais limpo o terminal: daemaon > ipfs.logs &. 

#sleep 5 #eu preciso dormir pra garantir que o deamon do ipfs esteja pronto pro peer rodar, talvez eu precise aumentar esse tempo
# Aguarda a resposta da API do IPFS
while ! curl -sf -X POST http://127.0.0.1:5001/api/v0/version > /dev/null; do
    sleep 1
done    

# Start your main foreground application (keeps container alive)
exec python3 /app/peer.py
#exec "$@"