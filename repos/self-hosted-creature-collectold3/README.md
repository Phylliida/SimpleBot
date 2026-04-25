# self-hosted-creature-collect
idk some self hosted creature collecting game

## Setup

`direnv allow` to load the nix shell (python + flask + tilemaker + cloudflared).

## Get `.osm.pbf` region files

Download per-country / per-state OSM extracts from **Geofabrik**:

**https://download.geofabrik.de/**

Navigate to the continent → country → (sub-region) and grab the `.osm.pbf`.
Sizes range from a few MB (small countries) to several GB (USA, Canada, etc.).
Daily-updated; pick the `-latest.osm.pbf` link. Drop the file into `osmpbf/`.

Tip: you can narrow a large extract before conversion with
`tilemaker --bbox minlon,minlat,maxlon,maxlat ...` to avoid baking 8 GB of tiles
you'll never pan to.

## Low-zoom land polygons (one-time, before first tile build)

```
./get-shapefiles.sh
```

Downloads `ne_10m_land` from Natural Earth (~1 MB) into `landcover/` so
tilemaker has continent polygons to emit at z0–z8. Without this the base map
looks mostly empty at zoomed-out views (OSM alone has almost no content at
low zoom — no land/water fill, just sparse country labels).

The script also has commented-out fetches for `ne_10m_urban_areas`,
`ne_10m_glaciated_areas`, and `ne_10m_antarctic_ice_shelves_polys` if you
ever want richer low-zoom rendering. Tilemaker silently skips any source
whose `.shp` file is missing, so this step is optional but strongly
recommended for the "save current view" flow (which pre-caches z0–z5 tiles
for the whole world).

## Convert `.osm.pbf` to tiles + POI index

```
./make-tiles.sh
```

For each `osmpbf/<name>.osm.pbf` this produces:
- `data/<name>.mbtiles` — vector tiles (via tilemaker). Uses
  `tilemaker-slim.json` + `tilemaker-slim.lua` — a trimmed openmaptiles
  schema that drops attributes/layers the client never reads (no
  brunnel/ramp/service/oneway on roads, no building heights, no
  mountain_peak/aeroway/waterway layers). If you changed anything in those
  files you need to `rm data/*.mbtiles` first to force a rebuild (already-
  built files are skipped).
- `data/<name>.pois.sqlite` — big server-side POI index via
  `osmium tags-filter n/name w/name` (covers nodes AND named ways like
  buildings), then `osmium export` → `build-poi-db.py`. Polygon features get a
  centroid. Stores lng, lat, name, category, and a JSON `props` blob (address,
  opening_hours, phone, website, wheelchair, brand, cuisine, description,
  wikipedia/wikidata, internet_access). Viewport lookups go through a
  `poi_rtree` spatial index (JOINed back to `poi` by rowid) — empty/sparse
  regions resolve in ~1 ms, populated regions in ~60 ms. If you have an older
  pre-rtree `.pois.sqlite`, run `python3 add-poi-rtree.py <file>` to migrate
  in place (~80 s for the 6.5 M-row North America DB, adds ~0.4 GB). The
  server auto-detects the rtree and falls back to flat lat/lng indexes for
  un-migrated DBs.
- `data/<name>.walk.sqlite` — pedestrian walk graph built from `w/highway`
  features. Nodes are stored with their OSM ids (so multi-region downloads
  can dedup at tile boundaries) plus rtree-indexed lng/lat; edges carry an
  integer-meter weight, a 3-m Douglas-Peucker-simplified shape blob, and an
  interned street-name id. Consumed by the offline walk+transit router.
- `data/<name>.routes.sqlite` — transit route geometry (bus/tram/subway/
  light_rail/monorail/train) for the map's route overlay.

Already-built files are skipped. On `/poi?bbox=`, the server does a fast
rtree-indexed spatial query (`poi_rtree JOIN poi ON rowid`) and returns a
compact binary bundle (`POIB` header + columnar lng/lat/name_idx/category_idx
typed arrays + a shared string pool + packed per-POI props). The client
parses the buffer into POI objects with all strings pooled, so repeated
values like `"Starbucks"` or `"restaurant"` share one JS string instance.
`/walk-graph?bbox=` emits an equivalent `WALK` binary bundle — edges sorted
by weight so most fit in a u8 column, name indices u8/u16/u32-packed based
on the region's unique-name count, and shapes concatenated once into a
single buffer referenced by offset/length.

Both bundles are stored in IndexedDB as raw `ArrayBuffer` per region,
bypassing JSON framing entirely.

## Transit schedules (GTFS)

For the in-app trip planner to do walk + transit routing, `data/schedule.sqlite`
needs to exist. It holds every agency's routes, trips, stop_times, calendars,
and authoritative route shapes (for drawing the actual bus path on the map).

### Just one agency (quick test)

```
./get-gtfs-stm.sh
python3 build-schedule-db.py stm gtfs/stm.zip data/schedule.sqlite
python3 link-gtfs-to-osm.py data/schedule.sqlite data/canada-260417.routes.sqlite
```

This gets you ~11 MB of DB covering STM (Montreal).

### All schedules (Canada + US, or any country)

Uses the [Mobility Database](https://mobilitydatabase.org) catalog of
~2,000 GTFS feeds worldwide. Pick a country code and the catalog filter script
emits a `feeds-<cc>.tsv` with slug / URL / name per feed.

```
# 1. Wipe the old schedule (the schema may have changed; incremental rebuild
#    of old feeds doesn't backfill new fields like GTFS shapes).
rm -f data/schedule.sqlite data/schedule.sqlite-shm data/schedule.sqlite-wal

# 2. Fetch a fresh Mobility Database catalog (updated ~monthly)
curl -sSL --max-time 120 https://files.mobilitydatabase.org/feeds_v2.csv \
  -o data/mdb-catalog.csv

# 3. Generate per-country feed lists
python3 get-gtfs-catalog.py --country CA --catalog data/mdb-catalog.csv \
  --output feeds-ca.tsv
python3 get-gtfs-catalog.py --country US --catalog data/mdb-catalog.csv \
  --output feeds-us.tsv

# 4. STM isn't registered in Mobility Database — append it manually if wanted
printf 'stm\thttps://www.stm.info/sites/default/files/gtfs/gtfs_stm.zip\tSTM\n' \
  >> feeds-ca.tsv

# 5. Ingest. Canada is ~100 feeds (~30 min); US is ~900 feeds (~2–3 hours).
#    Both stream: download one zip → validate → ingest → delete → next.
#    Peak disk = one zip at a time (~100-200 MB).
mkdir -p logs
python3 ingest-gtfs.py data/schedule.sqlite --feeds feeds-ca.tsv --tmp /tmp \
  2>&1 | tee logs/ingest-ca.log
python3 ingest-gtfs.py data/schedule.sqlite --feeds feeds-us.tsv --tmp /tmp \
  2>&1 | tee logs/ingest-us.log

# 6. Link GTFS stops to the OSM route_stop nodes (needed for "click stop → see
#    which bus is coming"). Pass every per-country routes.sqlite you have.
python3 link-gtfs-to-osm.py data/schedule.sqlite \
  data/canada-260417.routes.sqlite data/us-260417.routes.sqlite
```

**Validator**: `ingest-gtfs.py` runs `validate-gtfs.py` on each zip before
touching the DB. Feeds that fail validation get logged and skipped — nothing
corrupts the existing DB. Expect ~10% of feeds to be flagged (stale calendars,
missing `stop_times.txt`, broken references). You can inspect them with
`grep '^\[fail\]' logs/ingest-*.log`.

**Resumable**: `feed_meta` tracks each ingested slug. Re-running the same
`ingest-gtfs.py` command skips anything already done, so crashes or pauses
are harmless.

**Final sizes** (rough):
- ~100 Canadian feeds → ~80 MB schedule.sqlite (without shapes: ~30 MB;
  shapes add ~50 MB)
- ~900 US feeds on top → ~400 MB schedule.sqlite
- Peak during ingest: one GTFS zip in `/tmp` (~50-300 MB for big agencies)

### Other countries

Same pattern:

```
python3 get-gtfs-catalog.py --country GB --catalog data/mdb-catalog.csv \
  --output feeds-gb.tsv
python3 ingest-gtfs.py data/schedule.sqlite --feeds feeds-gb.tsv --tmp /tmp
```

Use ISO 3166-1 alpha-2 codes. Add `--include-auth` to include feeds that
require API keys (you'll need to handle those URLs separately).

## Download fonts (one-time, for labels/landmarks)

```
./get-fonts.sh
```

Shallow-clones `klokantech/klokantech-gl-fonts` (which ships pre-built glyph
PBFs) and copies **KlokanTech Noto Sans Regular** into `fonts/` (~3.7 MB).

## Download POI icons

```
./get-icons.sh
```

Shallow-clones `mapbox/maki` and copies ~215 SVG icons into `icons/` (~900 KB).
MapLibre registers them as style images; the `poi-icons` layer draws the
matching icon per feature (by `subclass`, falling back to `class`, then to a
red dot default marker).


That one fontstack is enough for road names, place names, and POI labels
(shops, cafés, schools, etc.). Without it, the map renders with no text.

Fonts are served at `/fonts/<stack>/<range>.pbf`. **The first `↓ save current
view` download also prefetches all font glyph ranges**, so after one save the
app is fully offline — no more font fetches on later zooms/pans.

## Run

```
python run.py
```

Listens on **port 8465**. Open http://localhost:8465 on the same machine.

## HTTPS is required for geolocation (a.k.a. the whole point of the game)

Browsers **silently block** `navigator.geolocation` (and thus the map's
"where am I" button) on plain HTTP *except* for `localhost`. Hitting the app
from your phone at `http://192.168.x.x:8465` → no permission prompt, just
failure. You need HTTPS for any remote access.

### Option A — quick cloudflared trial tunnel

Ephemeral URL, zero config, dies when you Ctrl-C:

```
cloudflared tunnel --url http://localhost:8465
```

Prints a `https://<random>.trycloudflare.com` URL — open that on your phone.

### Option B — named tunnel with your own domain

One-time setup:

```
cloudflared tunnel login                       # opens browser for auth
cloudflared tunnel create creature-collect
cloudflared tunnel route dns creature-collect poke.phylliidaassets.org
```

Write `~/.cloudflared/config.yml`:

```yaml
tunnel: creature-collect
credentials-file: /home/you/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: poke.phylliidaassets.org
    service: http://localhost:8465
  - service: http_status:404
```

Then:

```
cloudflared tunnel run creature-collect
```

### NixOS service (optional, for "always on")

If you run NixOS and want the tunnel to come up at boot, add to
`/etc/nixos/configuration.nix`:

```nix
services.cloudflared = {
  enable = true;
  tunnels."creature-collect" = {
    credentialsFile = "/var/lib/cloudflared/<tunnel-id>.json";
    ingress."poke.phylliidaassets.org" = "http://localhost:8465";
    default = "http_status:404";
  };
};
```

Copy the credentials JSON to `/var/lib/cloudflared/` and `nixos-rebuild switch`.

## On iPhone: install as a PWA

Open the HTTPS URL in Safari → tap **Share** → **Add to Home Screen**. Launch
from the home-screen icon (not from Safari) — this is what keeps offline tiles
from being evicted by iOS after ~7 days.
