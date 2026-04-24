#!/usr/bin/env bash
# GTFS download + ingest for South America. See get-transit-region.sh for
# the shared pipeline; this wrapper just pins the continent label and the
# ISO 3166-1 country codes we iterate.
set -euo pipefail
cd "$(dirname "$0")"
exec ./get-transit-region.sh "$@" south-america \
  AR BO BR CL CO EC FK GF GY PE PY SR UY VE
