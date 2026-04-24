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

cd "$(dirname "$0")/../../repos/ComfyUI"
exec ~/.venv/bin/python main.py --listen 0.0.0.0 --port 8188
