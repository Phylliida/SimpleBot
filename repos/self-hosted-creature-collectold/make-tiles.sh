#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

RESOURCES="$(dirname "$(dirname "$(readlink -f "$(which tilemaker)")")")/share/tilemaker"
mkdir -p data

shopt -s nullglob
found=0
for pbf in osmpbf/*.osm.pbf; do
  found=1
  name="$(basename "$pbf" .osm.pbf)"
  mbtiles="data/${name}.mbtiles"
  pois_db="data/${name}.pois.sqlite"

  if [ ! -e "$mbtiles" ]; then
    echo "==> tiles: $pbf -> $mbtiles"
    # Slim config/lua strip layers and fields the client never reads. The
    # lua dofile's the upstream process-openmaptiles.lua (via $TILEMAKER_SHARE)
    # so all upstream logic is reused.
    TILEMAKER_SHARE="$RESOURCES" tilemaker --input "$pbf" --output "$mbtiles" \
      --config tilemaker-slim.json \
      --process tilemaker-slim.lua
  else
    echo "skip tiles: $mbtiles already exists"
  fi

  if [ ! -e "$pois_db" ]; then
    echo "==> POIs: $pbf -> $pois_db"
    TMP=$(mktemp -d)
    osmium tags-filter "$pbf" n/name w/name \
      n/addr:housenumber w/addr:housenumber \
      n/addr:street w/addr:street \
      n/place=city,town,village,suburb,hamlet \
      w/place=city,town,village,suburb,hamlet \
      w/addr:interpolation \
      r/boundary=administrative \
      -o "$TMP/named.osm.pbf" --overwrite
    osmium export "$TMP/named.osm.pbf" -f geojsonseq \
      -o "$TMP/pois.geojsonseq" --overwrite
    python3 build-poi-db.py "$TMP/pois.geojsonseq" "$pois_db"
    rm -rf "$TMP"
  else
    echo "skip POIs: $pois_db already exists"
  fi

  walk_db="data/${name}.walk.sqlite"
  if [ ! -e "$walk_db" ]; then
    echo "==> walk graph: $pbf -> $walk_db"
    TMP=$(mktemp -d)
    osmium tags-filter "$pbf" w/highway \
      -o "$TMP/walk.osm.pbf" --overwrite
    python3 build-walk-graph.py "$TMP/walk.osm.pbf" "$walk_db"
    rm -rf "$TMP"
  else
    echo "skip walk graph: $walk_db already exists"
  fi

  hn_db="data/${name}.housenumbers.sqlite"
  if [ ! -e "$hn_db" ]; then
    echo "==> housenumbers: $pbf -> $hn_db"
    TMP=$(mktemp -d)
    osmium tags-filter "$pbf" n/addr:housenumber w/addr:housenumber \
      -o "$TMP/hn.osm.pbf" --overwrite
    python3 build-housenumbers.py "$TMP/hn.osm.pbf" "$hn_db"
    rm -rf "$TMP"
  else
    echo "skip housenumbers: $hn_db already exists"
  fi

  routes_db="data/${name}.routes.sqlite"
  if [ ! -e "$routes_db" ]; then
    echo "==> routes: $pbf -> $routes_db"
    TMP=$(mktemp -d)
    osmium tags-filter "$pbf" \
      r/route=bus,trolleybus,share_taxi,subway,tram,light_rail,train,monorail \
      -o "$TMP/routes.osm.pbf" --overwrite
    python3 build-routes-db.py "$TMP/routes.osm.pbf" "$routes_db"
    rm -rf "$TMP"
  else
    echo "skip routes: $routes_db already exists"
  fi
done

if [ "$found" = 0 ]; then
  echo "no .osm.pbf files found in osmpbf/"
fi

# Transit schedules (GTFS). Independent of per-PBF loop — one aggregated db.
schedule_db="data/schedule.sqlite"
shopt -s nullglob
gtfs_zips=(gtfs/*.zip)
if [ ${#gtfs_zips[@]} -gt 0 ]; then
  if [ ! -e "$schedule_db" ]; then
    echo "==> building schedule db from ${#gtfs_zips[@]} GTFS zip(s)"
    args=()
    for z in "${gtfs_zips[@]}"; do
      slug="$(basename "$z" .zip)"
      args+=("$slug" "$z")
    done
    python3 build-schedule-db.py "${args[@]}" "$schedule_db"

    # Link GTFS stops to OSM nodes for every routes.sqlite we built
    route_dbs=(data/*.routes.sqlite)
    if [ ${#route_dbs[@]} -gt 0 ]; then
      echo "==> linking GTFS stops to OSM nodes"
      python3 link-gtfs-to-osm.py "$schedule_db" "${route_dbs[@]}"
    fi
  else
    echo "skip schedule: $schedule_db already exists"
  fi
fi
