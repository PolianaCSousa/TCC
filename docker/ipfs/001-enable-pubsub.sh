#!/bin/sh
# O kubo executa os scripts de /container-init.d antes de subir o daemon.
# Sem isso, /api/v0/pubsub/pub e /sub respondem 404 e o rendezvous não funciona.
set -e

ipfs config --json Pubsub.Enabled true
