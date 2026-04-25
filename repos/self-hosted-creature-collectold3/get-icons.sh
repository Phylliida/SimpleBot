#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ -d icons ] && [ "$(ls icons 2>/dev/null | wc -l)" -gt 10 ]; then
  echo "icons/ already populated — delete it to re-fetch"
  exit 0
fi

mkdir -p icons
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "==> cloning mapbox/maki (shallow)"
git clone --depth 1 https://github.com/mapbox/maki.git "$TMP"

echo "==> copying SVGs → icons/"
cp "$TMP/icons/"*.svg icons/

echo "done. ($(ls icons | wc -l) icons, $(du -sh icons | cut -f1))"
