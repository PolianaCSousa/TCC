#!/bin/bash

# Start the background task/service
ipfs daemon & 

sleep 5

# Start your main foreground application (keeps container alive)
exec python3 /app/peer.py