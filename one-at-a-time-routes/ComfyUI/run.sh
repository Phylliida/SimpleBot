#!/usr/bin/env bash
set -euo pipefail


# If we're not already in Konsole, re-run this script inside Konsole (and keep it open).
if [[ -z "${IMRUNNING:-}" ]]; then
  export IMRUNNING=1
  exec konsole --noclose -e bash "$0" "$@"
fi

cd /home/bepis/prog/SimpleBot/repos/ComfyUI
exec ~/.venv/bin/python main.py --listen 0.0.0.0 --port 8188
