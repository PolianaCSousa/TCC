#!/bin/bash

# init at runtime: each container generates its own key and therefore its own PeerID
if [ ! -f "${IPFS_PATH:-/root/.ipfs}/config" ]; then
    ipfs init
    ipfs config --json Pubsub.Enabled true
fi

ipfs daemon &

# Waits for the IPFS API to respond
while ! curl -sf -X POST http://127.0.0.1:5001/api/v0/version > /dev/null; do
    sleep 1
done    

source /app/.env

exec python3 /app/peer.py