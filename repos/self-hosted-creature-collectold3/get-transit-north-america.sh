#!/usr/bin/env bash
# End-to-end pipeline for Canada + US + Mexico GTFS transit data.
#
# Produces data/schedule.sqlite populated with every registered feed in the
# Mobility Database plus Montreal's STM (which isn't in the catalog). Runs
# each step idempotently — re-running skips anything already finished, so
# it's safe to Ctrl-C and rerun.
#
# Resumes gracefully via ingest-gtfs.py's feed_meta table. Failed feeds are
# logged and skipped (not fatal). Expect 3-4 hours on a fresh run.
#
# Usage:
#   ./get-transit-north-america.sh                        # incremental
#   ./get-transit-north-america.sh --fresh                # wipe schedule first
#   ./get-transit-north-america.sh --refresh-catalog      # re-fetch mdb-catalog
set -euo pipefail
cd "$(dirname "$0")"

fresh=0
refresh_catalog=0
for arg in "$@"; do
  case "$arg" in
    --fresh) fresh=1 ;;
    --refresh-catalog) refresh_catalog=1 ;;
    -h|--help)
      sed -n '3,13p' "$0"
      exit 0
      ;;
    *) echo "unknown flag: $arg" >&2; exit 1 ;;
  esac
done

mkdir -p data logs

# --- 1. Wipe schedule on --fresh ------------------------------------------
if [ "$fresh" = 1 ]; then
  echo "==> wiping existing schedule (fresh rebuild)"
  rm -f data/schedule.sqlite data/schedule.sqlite-shm data/schedule.sqlite-wal
fi

# --- 2. Fetch Mobility Database catalog -----------------------------------
catalog="data/mdb-catalog.csv"
if [ ! -e "$catalog" ] || [ "$refresh_catalog" = 1 ]; then
  echo "==> fetching Mobility Database catalog -> $catalog"
  curl -sSL --max-time 300 https://files.mobilitydatabase.org/feeds_v2.csv \
    -o "$catalog"
else
  echo "skip catalog: $catalog already exists (pass --refresh-catalog to update)"
fi

# --- 3. Generate per-country feed lists -----------------------------------
for cc in CA US MX; do
  tsv="feeds-$(echo "$cc" | tr '[:upper:]' '[:lower:]').tsv"
  if [ ! -e "$tsv" ] || [ "$refresh_catalog" = 1 ]; then
    echo "==> filtering $cc feeds -> $tsv"
    python3 get-gtfs-catalog.py --country "$cc" --catalog "$catalog" \
      --output "$tsv"
  else
    echo "skip $tsv: already exists"
  fi
done

# --- 4. Append STM (not in the catalog) -----------------------------------
stm_line='stm	https://www.stm.info/sites/default/files/gtfs/gtfs_stm.zip	STM'
if ! grep -q $'^stm\t' feeds-ca.tsv; then
  echo "==> appending STM to feeds-ca.tsv"
  printf '%s\n' "$stm_line" >> feeds-ca.tsv
fi

# --- 5. Ingest each country's feeds ---------------------------------------
# ingest-gtfs.py streams one feed at a time (download -> validate -> ingest
# -> delete zip) and is resumable via the schedule db's feed_meta table, so
# re-running is cheap.
for cc in ca us mx; do
  tsv="feeds-${cc}.tsv"
  log="logs/ingest-${cc}.log"
  echo "==> ingesting ${cc^^} feeds (logging to $log)"
  # ingest-gtfs.py returns non-zero when ANY individual feed fails (download
  # 404s, validation errors, etc). Per-feed failures are expected — agencies
  # rotate URLs, temporarily take feeds offline, etc. — and shouldn't halt
  # the pipeline. `|| true` prevents `set -e` from aborting on that.
  python3 ingest-gtfs.py data/schedule.sqlite --feeds "$tsv" --tmp /tmp \
    2>&1 | tee "$log" || true
done

# --- 6. Link GTFS stops to OSM route_stop nodes ---------------------------
# Uses whatever data/*.routes.sqlite files exist (built by make-tiles.sh).
# Safe to skip if no routes.sqlite is present.
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
echo "done."
if [ -e data/schedule.sqlite ]; then
  size_mb=$(stat --printf='%s' data/schedule.sqlite | awk '{printf "%.1f", $1/1048576}')
  echo "data/schedule.sqlite = ${size_mb} MB"
  echo "failed feeds: $(grep -c '^\[fail\]' logs/ingest-*.log 2>/dev/null || echo 0)"
fi
