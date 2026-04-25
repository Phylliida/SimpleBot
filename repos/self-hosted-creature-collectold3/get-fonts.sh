#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

FONT="KlokanTech Noto Sans Regular"

if [ -d "fonts/$FONT" ]; then
  echo "fonts/$FONT already present — delete it to re-fetch"
  exit 0
fi

mkdir -p fonts
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "==> cloning klokantech/klokantech-gl-fonts (shallow)"
git clone --depth 1 https://github.com/klokantech/klokantech-gl-fonts.git "$TMP"

echo "==> copying '$FONT' → fonts/"
cp -r "$TMP/$FONT" fonts/

echo "done. ($(du -sh "fonts/$FONT" | cut -f1))"
