#!/usr/bin/env bash
# GTFS download + ingest for Oceania (Australia / New Zealand / Pacific).
# See get-transit-region.sh for the shared pipeline.
set -euo pipefail
cd "$(dirname "$0")"
exec ./get-transit-region.sh "$@" oceania \
  AS AU CK FJ FM GU KI MH MP NC NF NR NU NZ PF PG PW SB TK TO TV VU WF WS
