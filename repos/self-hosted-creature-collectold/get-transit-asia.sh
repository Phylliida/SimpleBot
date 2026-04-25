#!/usr/bin/env bash
# GTFS download + ingest for Asia. See get-transit-region.sh for the shared
# pipeline. Excludes Turkey / Russia / Cyprus — those live in the Europe
# script by convention; add them there or duplicate here if you prefer.
set -euo pipefail
cd "$(dirname "$0")"
exec ./get-transit-region.sh "$@" asia \
  AE AF AM AZ BD BH BN BT CN GE HK ID IL IN IQ IR JO JP KG KH KP KR \
  KW KZ LA LB LK MM MN MO MV MY NP OM PH PK PS QA SA SG SY TH TJ TL \
  TM TW UZ VN YE
