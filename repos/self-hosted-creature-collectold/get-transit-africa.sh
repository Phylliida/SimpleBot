#!/usr/bin/env bash
# GTFS download + ingest for Africa. See get-transit-region.sh for the
# shared pipeline. Many African countries have zero GTFS feeds in Mobility
# Database — they're included for completeness and iterate instantly.
set -euo pipefail
cd "$(dirname "$0")"
exec ./get-transit-region.sh "$@" africa \
  AO BF BI BJ BW CD CF CG CI CM CV DJ DZ EG EH ER ET GA GH GM GN GQ GW \
  KE KM LR LS LY MA MG ML MR MU MW MZ NA NE NG RE RW SC SD SL SN SO SS \
  ST SZ TD TG TN TZ UG YT ZA ZM ZW
