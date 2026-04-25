#!/usr/bin/env bash
# GTFS download + ingest for Europe. See get-transit-region.sh for the
# shared pipeline. Russia, Turkey and Cyprus are included here (common
# convention) even though parts overlap Asia geographically.
set -euo pipefail
cd "$(dirname "$0")"
exec ./get-transit-region.sh "$@" europe \
  AD AL AT BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GR HR HU IE \
  IM IS IT LI LT LU LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SK \
  SM TR UA VA XK
