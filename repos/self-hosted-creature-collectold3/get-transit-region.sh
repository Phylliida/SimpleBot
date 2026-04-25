#!/usr/bin/env bash
# Generic GTFS transit download/ingest pipeline for a named region.
# Called by the per-continent wrappers (get-transit-south-america.sh etc.).
# North America has its own hand-tuned script with an STM-specific append.
#
# Usage:
#   ./get-transit-region.sh [--fresh] [--refresh-catalog] LABEL CC1 CC2 ...
#
#   LABEL    — short name used in log and TSV filenames (e.g. "south-america")
#   CCn      — ISO 3166-1 alpha-2 country codes, one per country to include
set -euo pipefail
cd "$(dirname "$0")"

fresh=0
refresh_catalog=0
while [ $# -gt 0 ]; do
  case "$1" in
    --fresh) fresh=1; shift ;;
    --refresh-catalog) refresh_catalog=1; shift ;;
    -h|--help) sed -n '3,13p' "$0"; exit 0 ;;
    --) shift; break ;;
    -*) echo "unknown flag: $1" >&2; exit 1 ;;
    *) break ;;
  esac
done

if [ $# -lt 2 ]; then
  echo "usage: $0 [--fresh] [--refresh-catalog] LABEL CC1 [CC2 ...]" >&2
  exit 1
fi

label="$1"; shift
country_codes=("$@")

mkdir -p data logs

# --- wipe on --fresh ------------------------------------------------------
if [ "$fresh" = 1 ]; then
  echo "==> wiping existing schedule (fresh rebuild)"
  rm -f data/schedule.sqlite data/schedule.sqlite-shm data/schedule.sqlite-wal
fi

# --- Mobility Database catalog -------------------------------------------
catalog="data/mdb-catalog.csv"
if [ ! -e "$catalog" ] || [ "$refresh_catalog" = 1 ]; then
  echo "==> fetching Mobility Database catalog -> $catalog"
  curl -sSL --max-time 300 https://files.mobilitydatabase.org/feeds_v2.csv \
    -o "$catalog"
else
  echo "skip catalog: $catalog already exists (pass --refresh-catalog to update)"
fi

# --- per-country filtering, concatenated into one feeds-<label>.tsv ------
combined="feeds-${label}.tsv"
if [ ! -e "$combined" ] || [ "$refresh_catalog" = 1 ]; then
  echo "==> filtering ${#country_codes[@]} country code(s) -> $combined"
  : > "$combined"
  for cc in "${country_codes[@]}"; do
    python3 get-gtfs-catalog.py --country "$cc" --catalog "$catalog" --output - \
      >> "$combined"
  done
  echo "    $(wc -l < "$combined") total feeds in $combined"
else
  echo "skip $combined: already exists (pass --refresh-catalog to regenerate)"
fi

# --- ingest ---------------------------------------------------------------
log="logs/ingest-${label}.log"
echo "==> ingesting ${label} feeds (logging to $log)"
python3 ingest-gtfs.py data/schedule.sqlite --feeds "$combined" --tmp /tmp \
  2>&1 | tee "$log"

# --- link GTFS stops to OSM route_stop nodes -----------------------------
shopt -s nullglob
route_dbs=(data/*.routes.sqlite)
shopt -u nullglob
if [ ${#route_dbs[@]} -gt 0 ]; then
  echo "==> linking GTFS stops to OSM nodes across ${#route_dbs[@]} routes db(s)"
  python3 link-gtfs-to-osm.py data/schedule.sqlite "${route_dbs[@]}"
else
  echo "skip link: no data/*.routes.sqlite yet (run ./make-tiles.sh first)"
fi

echo
echo "done ($label)."
if [ -e data/schedule.sqlite ]; then
  size_mb=$(stat --printf='%s' data/schedule.sqlite | awk '{printf "%.1f", $1/1048576}')
  echo "data/schedule.sqlite = ${size_mb} MB"
  echo "failed feeds in this run: $(grep -c '^\[fail\]' "$log" 2>/dev/null || echo 0)"
fi
