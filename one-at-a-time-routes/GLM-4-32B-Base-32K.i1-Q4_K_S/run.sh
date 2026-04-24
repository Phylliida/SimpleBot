#!/usr/bin/env bash
set -euo pipefail


# If we're not already in Konsole, re-run this script inside Konsole (and keep it open).
if [[ -z "${IMRUNNING:-}" ]]; then
  export IMRUNNING=1
  # Start konsole minimized via KWin scripting. After the exec below, konsole
  # inherits this shell's PID ($$), which the helper uses to match the window.
  "$(dirname "$0")/../../bin/minimize-konsole.sh" "$$" >/dev/null 2>&1 &
  exec konsole --noclose -e bash "$0" "$@"
fi

cd "$(dirname "$0")"
nix-shell -p  llama-cpp-vulkan --run 'llama-server -m ../../models/GLM-4-32B-Base-32K.i1-Q4_K_S.gguf --ignore-eos --jinja --chat-template "message.content" --ctx-size 12000 --temp 1 --top-k 0 --top-p 1 --min-p 0 --port 4032 --gpu-layers 999 --host 0.0.0.0'
