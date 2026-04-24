#!/usr/bin/env bash
# Fetch the Natural Earth land polygons used by tilemaker-slim.json for the
# low-zoom (<=8) land fill. Without this the base map is mostly empty beige
# when zoomed out past z~5 because OSM-derived features are too sparse.
#
# Only ne_10m_land is fetched by default. The config also references
# ne_10m_urban_areas / ne_10m_glaciated_areas / ne_10m_antarctic_ice_shelves
# and coastline/water_polygons — tilemaker skips missing sources, so you can
# stop here unless you want richer low-zoom rendering.
set -euo pipefail
cd "$(dirname "$0")"

fetch_ne() {
  local name="$1"   # e.g. ne_10m_land
  local kind="$2"   # physical | cultural
  local dest="landcover/${name}"
  if [ -e "${dest}/${name}.shp" ]; then
    echo "skip: ${dest}/${name}.shp already exists"
    return
  fi
  mkdir -p "$dest"
  local tmp; tmp="$(mktemp -d)"
  local url="https://naciscdn.org/naturalearth/10m/${kind}/${name}.zip"
  echo "==> downloading $url"
  curl -sSfL -o "$tmp/${name}.zip" "$url"
  unzip -q -o "$tmp/${name}.zip" -d "$dest"
  rm -rf "$tmp"
  echo "==> extracted to $dest/"
}

fetch_ne ne_10m_land                      physical
fetch_ne ne_10m_ocean                     physical
fetch_ne ne_10m_urban_areas               cultural
fetch_ne ne_10m_glaciated_areas           physical
fetch_ne ne_10m_antarctic_ice_shelves_polys physical

echo
echo "Done. Now run ./make-tiles.sh (delete data/*.mbtiles first to force a rebuild)."
