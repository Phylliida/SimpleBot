# Session handoff

Long session touching memory/disk optimisation, GTFS ingest hardening, and a
full medieval-theme refactor with generalised UI variables. The map works,
all wipes work, tiles + walk graph + POIs + housenumbers all ship as binary
bundles. This note is for whoever (including a future Claude) picks this up.

## Files added this session

| File | Role |
|---|---|
| `bench-walk.py` | Benches `/walk-graph` for a bbox, parses the WALK binary, prints per-column byte breakdown + hypothetical wins. |
| `bench-tiles.py` | Reads `data/*.mbtiles` directly, per-layer byte table + per-zoom distribution + top-N heaviest tiles. |
| `build-housenumbers.py` | Extracts addr:housenumber nodes/ways → `data/<name>.housenumbers.sqlite` with rtree. |
| `get-shapefiles.sh` | Downloads Natural Earth land/ocean/urban/glacier/ice-shelf into `landcover/*`. Run once. |
| `get-transit-region.sh` | Generic GTFS pipeline (catalog → filter → ingest → link). Takes `LABEL CC1 CC2 …`. |
| `get-transit-{north-america,south-america,europe,asia,africa,oceania}.sh` | Thin wrappers over `get-transit-region.sh` with per-continent country code lists. North America is its own hand-tuned script (STM append). |

## Binary bundles in play

Three separate binary formats are now served from the server and stored in
IndexedDB (one ArrayBuffer per region, `{regionId, buffer}` records).

### WALK bundle (`cc.walk.v5`, magic `WALK`, version 3)
Server: `/walk-graph?bbox=`. Size for a Montreal-ish bbox: ~20 MB.
- Header 48 B: magic, version, N, E, M (names), namesByteLen, shapesByteLen, u8End, u16End, nameIdxWidth, shapeEdgeCount, reserved.
- Nodes: N × (f64 osm_id, f32 lng, f32 lat) laid out as three parallel columns.
- Edges 4-byte cols: E × (u32 from, u32 to). `shape_off` / `shape_len` are *not* per-edge here — they're sparse (see below).
- **Weight cascade** — edges are sorted ascending by weight. Two split indices in the header say where u8 weights end and u16 weights end. ~98% of pedestrian edges are ≤255 m so they fit in u8. Client materialises back to `Float32Array(E)` at load.
- **name_idx** — 1/2/4-byte fixed width based on the region's unique-name count. Sentinel (all ones) = "no name".
- **Shapes** — has-shape bitmap (1 bit/edge, aligned to 4) + sparse `(u32 off, u16 len)` pairs for only the ~10% of edges that actually have a shape blob. Saves ~5 MB vs a dense `(off, len)` column.
- Client: `viewWalkRegion(buffer)` materialises typed-array views + dense `edgeShapeOff/Len` arrays.

### POI bundle (`cc.pois.v2`, magic `POIB`, version 1)
Server: `/poi?bbox=`. Size: ~5 MB (Montreal bbox).
- Header 32 B.
- Columns: N × f32 lng, N × f32 lat, N × i32 name_idx, N × i32 category_idx, N × u32 props_off (0xFFFFFFFF = none).
- Shared string pool: M × (u16 len + utf8). Every POI's name/category/props value is an index into this pool, so duplicate strings ("Starbucks", street names) occupy one slot instead of N.
- Per-POI props: u8 field_count, then field_count × (u8 field_code, u32 string_idx). `POI_FIELDS` array is hardcoded in both `run.py` and `static/index.html` and MUST stay in order — adding fields is append-only.
- Client hydrates into flat POI objects where every string is a reference into the pool (hidden-class stable, heavy string dedup).

### Housenumbers bundle (`cc.housenumbers.v1`, magic `HSNB`, version 1)
Server: `/housenumbers?bbox=`. Size: ~4 MB for ~600k housenumber points.
- Header 40 B: magic, version, N, M, stringsByteLen, reserved, f32 bbox W/S/E/N.
- Columns: N × u16 lng_q, N × u16 lat_q, N × u16 str_idx. Coords are quantised into the bbox at u16 resolution (~1 m/axis for a city-sized region).
- String pool: M × (u16 len + utf8).
- Client: **lazy-parsed** at first zoom ≥ 16 (see `ensureHousenumbersLoaded`). Parsed regions are sorted by lng so a `moveend` handler can binary-search the visible band and push only ~100 visible features into a `housenumbers` GeoJSON source for MapLibre. Rendered via the existing `housenumbers` symbol layer at minzoom 17.
- **Tilemaker no longer emits a housenumber layer.** That layer was ~12 MB of z14 tile bytes (~15%) for features that only render at z17+. Moved out entirely.

## Server perf hotspots fixed

`/walk-graph` went from 44 s cold to ~7 s warm on a Canada bbox. The wins were:

1. **Don't `SELECT MIN/MAX FROM rtree` to check overlap.** Scanning a 10M-row rtree for MIN/MAX is 30+ seconds on a cold open. Replaced with a direct `SELECT 1 FROM rtree WHERE minX ≤ ? AND maxX ≥ ? AND minY ≤ ? AND maxY ≥ ? LIMIT 1` which uses the spatial index. Same fix applied to POI/routes (`_rtree_overlaps`, `_poi_overlaps` in `run.py`).
2. **Split `WHERE from_id IN bbox OR to_id IN bbox` into a UNION ALL** with each half using its own index. SQLite's OR planner was doing a full walk_edge scan.
3. **JOIN walk_node into the edges query** instead of a second pass to fetch coords.
4. **Stream via `conn.execute(...)` rather than `.fetchall()`**, fold the per-row column building into the same loop. Avoids materialising a 800k-tuple intermediate list.
5. **Pre-bind `.get` / `.append`** of the hot dicts/lists as local variables in the per-row loop — meaningful for 800k+ iterations.
6. **`gzip compresslevel=1`** everywhere. The binary is already dense, higher levels spend 3× the CPU for < 10% size drop.

Timing phases logged for any request carrying `X-Download: 1`:
```
[dl] GET /walk-graph?bbox=… size=9.9MB total=7.3s setup=2ms sql_bbox=0.5s process=5.7s pack=0.4s gzip=0.4s
```

## Theme system

Entirely driven by CSS variables now. Each theme's `ui` field maps to
`--ui-*` variables set at `documentElement.style` in `applyTheme`:

```js
--ui-bg, --ui-text, --ui-muted, --ui-border, --ui-hairline, --ui-hover,
--ui-input-bg, --ui-radius, --ui-shadow,
--ui-accent, --ui-accent-text, --ui-accent-border, --ui-danger
```

All panels/modals/cards/buttons/chips/inputs/dropdowns/map controls use
these vars. Default values live in `:root` so themes that don't fill in a
field still render sensibly. New theme = just add a new entry to `THEMES`
and an `<option>` to `#themeSelect` — no CSS edits needed.

**Medieval-only decoration** (serif body, small-caps headings, monospace
"ledger" numbers, sepia SVG icon filter, italic serif "i" on the attribution
tile, dashed stop timeline) stays scoped to `html[data-theme="medieval"]`.

**Medieval is the default** for new users (`|| 'medieval'` fallback in three
places where `cc.theme` is read). Existing users keep their stored choice.

**Watch for CSS cascade gotchas.** Several original rules (`#poiCardDirections`, `.mode-chip.on`, `.route-popup .*.on`, `#pinBar button.primary`, `.temp-pin`) had hardcoded accent colours and lived *after* the new var-based rules in the file. Same specificity = later wins. Already fixed but if any regressions reappear under "some button is blue again", check for more hardcoded `#3b7fdf` / `#4a90e2`.

## Welcome overlay

New first-run modal (`#welcome`, gated by `!localStorage.getItem('cc.welcomeSeen')`). Triggers the existing `appDataBtn` download via `.click()`, mirrors its progress status into the overlay via a MutationObserver, reloads on `cc:app-data-ready` event (dispatched at end of the app-data handler). Sets `cc.welcomeSeen=1` only after a successful download.

## Settings panel additions

- **"Show offline maps panel" toggle** — hides `#panel` (the offline-maps dropdown at bottom) when off. Persisted in `localStorage.cc.hidePanel`.
- **Attribution line at the bottom** of the settings sheet (Map data © OSM / MapLibre / Natural Earth). The built-in MapLibre attribution is in compact mode (small "i" tile) and themed to match the medieval palette.

## Wipe flow

Rewritten as single-phase: click → unregister SW → clear all Cache API entries → enumerate and delete every IDB (`indexedDB.databases()` + a hardcoded fallback list for Firefox) → `localStorage.clear()` → `location.reload()`. Status line in `#status` shows live progress, console gets a `[wipe] summary:` block.

**iOS PWA caveat**: `navigator.storage.estimate().usage` is unreliable on iOS — ignore it. The real per-region totals come from the per-region measurement in the offline-maps UI. Fully resetting requires uninstalling the home-screen icon (some WebKit-level HTTP caches are unreachable from JS).

To stop iOS hard-refresh from growing storage ~1 MB per reload: SW install handler uses `cache: 'no-store'` on fetches (not `'reload'`) so Safari doesn't shadow the Cache-API copies in its HTTP cache. MapLibre is self-hosted under `/static/vendor/` with `Cache-Control: no-store` for the same reason.

## GTFS ingest hardening

`ingest-gtfs.py` now:
1. Does a zip-magic-byte check after download. Some mirrors return 200 OK with an empty body or HTML error page; `try_download()` catches those.
2. **Retries with the MD mirror URL** (4th column of `feeds-*.tsv`) if the primary URL fails or delivers a non-zip body. `get-gtfs-catalog.py` emits the mirror as the 4th field.
3. **Pre-sorts `stop_times.txt` in the zip** if it isn't grouped by trip_id. `build-schedule-db.py` streams stop_times and flushes per trip_id transition, so unsorted input previously produced duplicate-ID pattern rows — ~45 feeds failed on this alone.
4. `validate-gtfs.py` downgraded the following errors to warnings (they're handled fine downstream):
   - stop-coordinates unparseable (build-schedule-db skips those rows anyway)
   - route_type non-integer (coerced to 0 in build)
   - stop-times-sorted-by-trip (now auto-sorted at ingest time)
   - referential-integrity misses (build drops affected trips)

After these changes Canada went from 26/132 failures to 8/132, and most remaining failures are dead URLs without a mirror.

## Build / rebuild commands

```
./get-shapefiles.sh           # one-time NE + water polygons
./make-tiles.sh               # per-PBF: mbtiles + pois + walk + routes + housenumbers.sqlite
./get-transit-north-america.sh --refresh-catalog   # full CA+US GTFS ingest, ~3h fresh
./get-transit-europe.sh       # etc. for other continents
python run.py                 # Flask server on :8465
```

Rebuild tiles when `tilemaker-slim.json/lua` changes:
```
rm data/*.mbtiles
./make-tiles.sh
```

## Bench commands

```
python3 bench-walk.py                         # prints WALK bundle byte breakdown
python3 bench-walk.py --url https://poke.phylliidaassets.org
python3 bench-tiles.py                        # reads data/*.mbtiles directly, per-layer table
python3 bench-tiles.py --minzoom 1 --maxzoom 5   # inspect low-zoom tiles
```

## Tilemaker simplify — DON'T

Earlier this session we tried `simplify_below: 15, simplify_level: 0.001`
across transportation, building, landcover, landuse, park to save ~18 MB
wire. It **glitched**:
- Road polylines jaggy at tile boundaries (simplified endpoints snapped inconsistently across adjacent tiles).
- Buildings collapsed from 708k features → 8k (tilemaker's `combine_below: 14` merged simplified polygons sharing edges, producing one super-blob per block).

Current state: transportation/transportation_name/building have no
simplification beyond defaults. Landcover/landuse/park keep the pre-refactor
defaults (`simplify_below: 13, simplify_level: 0.0003, ratio: 2`). If you
try again, leave **building alone** and use simplify_level ≤ 0.0005 on
polygons-only.

## Config / constants that must stay in sync

- `POI_FIELDS` array — order must match between `run.py` and `static/index.html`. Append-only (would break decoding of already-saved POIs).
- Binary format versions — `cc.walk.v5` / `cc.pois.v2` / `cc.housenumbers.v1` IDB names. If you change the format, bump the DB name AND the version field in the header.
- `THEMES` entries must all include a `ui: {}` dict with the same keys so `applyTheme` can set every `--ui-*` variable.

## Things the user was evaluating when context ran out

- Just finished a round of medieval-theme polish (bubbles/buttons/modals all themed, then generalised via `--ui-*`, then a regression where several buttons went blue again was fixed by finding hardcoded `#3b7fdf` / `#4a90e2` values in rules later in the stylesheet than the generic CTA rule).
- The medieval-theme default is active but only for users without a stored `cc.theme` in localStorage. Existing users keep their choice unless they clear data.

## Good next steps

- Write `bench-pois.py` analogous to `bench-walk.py`/`bench-tiles.py` if
  someone wants to iterate on the POI binary format.
- Consider adding `cc.transit.v1` theme (or other continent-appropriate
  palettes) by copying an existing `ui:` block.
- The wipe button could offer a "keep fonts/icons" mode so re-downloading
  app data after every wipe isn't required.
- The validator's remaining errors (required-files missing, bad zip from
  catalog URLs) could be logged more helpfully in a dashboard.

## Known-but-accepted quirks

- `.maplibregl-ctrl-attrib-button`'s "i" only centers when the attribution
  control is instantiated with `{ compact: true }` (hardcoded in the
  `new maplibregl.Map({ attributionControl: { compact: true }, ... })`
  options). If that's ever changed, the tile will stretch into a bar.
- `storage.estimate()` is lying on iOS; use the per-region UI numbers.
- On iOS Safari home-screen PWA, hard refresh in dev adds ~1 MB to the
  WebKit HTTP cache per reload. Fix is purely "uninstall the icon" —
  WebKit-level caches are unreachable from JS.
- Walk graph `process` phase stops at ~5 s for Canada-sized queries on
  cold sqlite — that's the SQLite JOIN + Python tuple unpacking, already
  streamed. Further wins would need pre-baking (build-time tiling) which
  we deferred.
