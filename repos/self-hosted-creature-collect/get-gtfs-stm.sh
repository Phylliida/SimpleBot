#!/usr/bin/env bash
# Downloads the Société de transport de Montréal GTFS feed.
# URL is stable but can be overridden via STM_GTFS_URL env var.
set -euo pipefail
cd "$(dirname "$0")"

URL="${STM_GTFS_URL:-https://www.stm.info/sites/default/files/gtfs/gtfs_stm.zip}"
DEST="gtfs/stm.zip"

mkdir -p gtfs
echo "==> downloading STM GTFS from $URL"
curl -fsSL --retry 3 -o "$DEST.tmp" "$URL"
mv "$DEST.tmp" "$DEST"
ls -lh "$DEST"
