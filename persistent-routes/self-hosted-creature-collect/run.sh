#!/usr/bin/env bash
set -euo pipefail
cd /home/bepis/prog/SimpleBot/repos/self-hosted-creature-collect
exec nix-shell --run 'python run.py'
