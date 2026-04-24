#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../repos/self-hosted-creature-collect"
exec nix-shell --run 'python run.py'
