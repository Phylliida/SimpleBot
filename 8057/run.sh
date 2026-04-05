#!/usr/bin/env bash
set -euo pipefail


# If we're not already in Konsole, re-run this script inside Konsole (and keep it open).
if [[ -z "${IMRUNNING:-}" ]]; then
  export IMRUNNING=1
  exec konsole --noclose -e bash "$0" "$@"
fi

nix-shell -p  llama-cpp-vulkan --run 'llama-server -m /home/bepis/chungus/ablateawaredata/GLM-ablated-aware-Q4_K_S.gguf --ignore-eos --jinja --chat-template "message.content" --ctx-size 12000 --temp 1 --top-k 0 --top-p 1 --min-p 0 --port 8056 --gpu-layers 999 --host 0.0.0.0'
