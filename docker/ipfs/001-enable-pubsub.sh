#!/bin/sh
# kubo runs the scripts in /container-init.d before starting the daemon
set -e

ipfs config --json Pubsub.Enabled true
