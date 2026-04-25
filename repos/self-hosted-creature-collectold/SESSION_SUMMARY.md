# Creature-Collect — Session Summary

Comprehensive write-up of what was built in this session, how it fits together, the
design philosophy that shaped it, and what's still open. Intended as a brief for a
future session or a new collaborator.

## What the app is

A self-hosted offline-first PWA for a stamp/creature-collect style map game.
Runs on a Flask server tunnelled through Cloudflare at
`https://poke.phylliidaassets.org`, listening on `localhost:8465`. The client
is one `static/index.html` + `static/sw.js` + `static/manifest.webmanifest` +
`static/icon.svg`. No build step, no bundler.

The goal is: download OSM data once on wifi, then use the phone like a
working map — locating yourself, searching, following directions,
flagging favorites — with **strict zero cellular data** after the initial
download.

## The "zero fetch except on explicit button" invariant

This is the most important design constraint, and it permeates everything.

Two buttons, and only two, may cause the client to hit the network:

1. **`↓ save current view`** — downloads tiles + POIs + transit routes + walking
   graph for the current bbox. Tiles go to Cache API (`TILES_CACHE`); POIs, routes,
   walking graph go to IndexedDB.
2. **`↓ app data`** — downloads fonts + icons + low-zoom base map tiles.
   One-time global setup; subsequent taps are idempotent.

A third button (`↻ refresh`) exists for pulling code updates, and the user
accepts that as a separate class of action.

Implementation: the service worker treats every resource-path prefix
(`/tiles/`, `/fonts`, `/icons`, `/poi`, `/routes`, `/walk-graph`) as
**cache-only** by default. A fetch without `X-Download: 1` gets either a
cached response or a 204 empty response — it never reaches the network.
Both buttons set `X-Download: 1` on their fetches, and the SW short-circuits
those to pass straight through to the network + cache the result.

MapLibre's tile/font/icon loads have no idea any of this is happening; they
fetch normally and the SW intercepts locally.

## Architecture

### Build pipeline (`./make-tiles.sh`)

For every `osmpbf/*.osm.pbf` dropped in:

1. **`tilemaker`** → `data/<name>.mbtiles` — OpenMapTiles-schema vector tiles
2. **POI index** via `osmium tags-filter` + `build-poi-db.py`:
   - Keep nodes/ways tagged `name`, `addr:housenumber`, `addr:street`,
     `place=city/town/village/suburb/hamlet`, ways with `addr:interpolation`,
     and `boundary=administrative` relations.
   - `build-poi-db.py` uses shapely to:
     - Load all addressed features into an R-tree
     - Load all place features into a second R-tree
     - Load all `addr:interpolation` ways and endpoint housenumber nodes
     - Load all boundary polygons (admin_level 2/4/6/8) and build an STRtree
   - For each named POI, fill missing address fields via a chain:
     `own addr` → `nearest addressed feature ≤160 m` → `street interpolation ≤50 m`
     → `point-in-polygon against boundary polygons` → `nearest-city ≤30 km`
   - Output: `data/<name>.pois.sqlite` with `poi(lng, lat, name, category, props JSON)`
3. **Transit route extraction** via `build-routes-db.py` using **pyosmium**:
   - `osmium tags-filter` keeps `route=bus,trolleybus,share_taxi,subway,tram,light_rail,train,monorail`
   - Two-pass pyosmium reader: pass 1 collects route relations + way→route associations,
     pass 2 reads ways with `locations=True` to emit segments
   - Output: `data/<name>.routes.sqlite` with `route_data(id, coords, route_ids)`
     + `route_meta(id, ref, name, network, operator, colour, mode)` + R-tree index
4. **Walking graph** via `build-walk-graph.py`:
   - `osmium tags-filter` keeps ways tagged `highway=*` (pedestrian-friendly classes)
   - pyosmium reads with `locations=True`, emits (node, edge) pairs; edges are
     bidirectional, weighted by haversine distance
   - Output: `data/<name>.walk.sqlite` with `walk_node(id, lng, lat)` + R-tree +
     `walk_edge(from_id, to_id, weight, name)`

### Server (`run.py`)

Flask, single file, ~200 lines. Endpoints:

- `/` — serves index.html with `Cache-Control: no-store` so the SW always sees the
  latest app shell on first fetch.
- `/sw.js` — same (no-store) so SW update detection works.
- `/manifest.webmanifest`, `/static/icon.svg` — app shell
- `/tiles/<z>/<x>/<y>.pbf` — reads every `data/*.mbtiles`, returns the one with the
  largest tile body (multi-country disambiguation; first-match was wrong at z0-5
  where both Canada and US have world tiles).
- `/poi?bbox=` — queries `data/*.pois.sqlite` files whose overall bounds intersect
  the query bbox, returns matching rows.
- `/routes?bbox=` — same pattern for routes; returns features + compound-IDed
  route metadata map (`canada-260417:42` etc).
- `/walk-graph?bbox=` — same for walk graphs; uses temp-table join + gzip
  response; streams tend to be 5–20 MB per city bbox.
- `/fonts/<stack>/<range>.pbf`, `/fontslist/<stack>` — glyph PBFs
- `/icons/<name>.svg`, `/iconslist` — Maki icons

Common pattern: `_cached_bounds(path, key, sql)` + `_relevant_files(glob, bounds_fn, bbox)`
pre-compute each sqlite file's overall coverage and cache, so a Toronto query
skips US files entirely (and vice versa).

### Client (`static/index.html`, single file)

Everything lives in one HTML file. The script is wrapped in an async IIFE
gated on `navigator.serviceWorker.ready` — the map literally cannot construct
until the SW is controlling the page, so no tile fetch ever slips through
uncontrolled.

Subsystems, in rough dependency order:

- **IndexedDB stores** — `cc.pois.v1` (POIs per-region), `cc.routes.v2` (routes
  + meta), `cc.walk.v1` (walk graph per-region), `cc.favorites.v1`
  (favorites, shared across regions)
- **localStorage** — `cc.regions.v1` (list of saved regions), `cc.view.v1`
  (last map view), `cc.selected.v1` (currently-selected POI),
  `cc.theme` + `cc.themeCustom`, `cc.units`, `cc.transit.v2`
  (modes + routes), `cc.fontsDownloaded`/`cc.iconsDownloaded`/`cc.baseMapDownloaded`,
  `cc.installDismissed`, `cc.geoPromptDone`
- **Map** — MapLibre GL 4.7 loaded from unpkg, cached in SW
- **Style sources** — `base` (maxzoom 5, for overzoom fallback), `local`
  (maxzoom 14, full detail), `bus-routes` (GeoJSON for per-route overlays),
  `walk-route` (GeoJSON for directions result)
- **Theme system** — 5 presets + custom (7 color pickers). POI icon color
  applied via canvas `source-in` composite during SVG rasterization;
  label color via CSS custom properties. Theme change triggers icon
  re-rasterization.
- **Search** — text box + type dropdown. Virtualized (windowed) list with
  52 px row height, ~3 overscan, rAF-throttled scroll. Uses substring match
  over in-memory `allPois` + haversine sort by distance from user location
  or map center.
- **Transit menu** — modal with per-mode chips + searchable per-route checkboxes.
  Routes default off; checking one auto-enables its mode. "All on/off" buttons
  cascade to both modes and routes.
- **Favorites** — selectable POI → ★ button → modal picks from all Maki
  icons + 8-color palette. Stored in IDB. Rendered as scaled colored bubbles
  with white-tinted icon + name label beneath. When a favorited POI is
  selected, the selection marker takes the favorite's color/icon.
- **Pin-drop** — pin control button → draggable blue pin at user location or
  viewport center → confirm → prompts name → saved as a favorite.
- **Walking directions** — "🚶 Walking directions from my location" button on
  the POI card. A* over the in-memory merged walk graph (haversine
  heuristic + binary min-heap). Path → GeoJSON LineString → blue route layer.
  Turn-by-turn instructions derived from bearing deltas at intersections.
- **Transit route click** — tapping a rendered route line opens a popup
  listing every route using that segment (with live checkbox state).

### UI layout

- **Top**: search input + type filter (z-index 15)
- **Top overlay (rare)**: install-to-home-screen banner (iOS, z-index 20)
- **Bottom-left**: scale control
- **Bottom-right control stack** (in order): settings gear, pin-drop,
  refresh, transit menu, MapLibre nav (+/−/compass), geolocate
- **Bottom**: offline-maps panel (`left: 8px; right: 68px` to leave room
  for the controls)
- **Floating**: POI selection card (top), directions panel (bottom),
  modal overlays (settings, favorites, transit menu)

## What was built in this session (roughly chronological)

1. Initial scaffolding — Flask + MapLibre + SW with cache-only tile handling.
2. Cloudflared tunnel setup docs.
3. Apple PWA install hint, safe-area insets, app icon SVG.
4. Save-region with explicit `X-Download` bypass — strict no-auto-fetch invariant.
5. IDB POI index + search UI with type filter + haversine-sorted results
   + virtualization (pretext-inspired windowing, implemented as vanilla JS).
6. Font bundling via KlokanTech fonts repo + glyph URL in the style.
7. Maki icon bundling + per-category icon mapping with alias map for
   OpenMapTiles `subclass` naming (underscore-hyphen conversion + ~60
   explicit aliases).
8. Favorites — IDB store, full-icon picker, colored bubble markers with
   name labels, per-favorite color/icon.
9. Pin-drop — draggable pin → favorite.
10. Settings panel — theme presets + custom theme + units toggle +
    export/import (JSON backup/restore).
11. Base-map overzoom — second source with maxzoom 5 so country/state/city
    labels + highways + water persist when zooming into undownloaded regions.
12. POI address enrichment — started with just-nearby-addresses, then escalated
    to full shapely pipeline with street interpolation + admin-polygon PIP.
13. Transit filtering — two passes: first per-mode toggles; then full
    relation-backed per-route filtering via pyosmium rewrite of
    build-routes-db.py.
14. Walking router — pyosmium-extracted graph, per-region download, in-memory
    A* with haversine heuristic.
15. Route-line click popup — tap a transit line, see all routes through
    that segment with per-route toggles.
16. Performance passes — gzip responses, file-bounds cache so bbox queries
    skip non-intersecting sqlite files, temp-table joins instead of chunked
    `IN`s, progress bars on walk-graph download.

## Design philosophy

### Offline-first is load-bearing

Every feature was designed around "works entirely offline after one download".
Things you might naively add — auto-updating data, server-side search,
real-time sync — all had to be rejected or redesigned. The discipline
pays off: the app genuinely works on airplane mode.

### Button-gated network is not negotiable

When I tried to "helpfully" pre-fetch small things (font updates, auto-refresh,
lazy loading on `styleimagemissing`), the user pushed back every time. The
rule is absolute: the service worker must not initiate a network request
except when the user has directly asked for one. Treat even SW update checks
as a violation worth questioning.

### Each sqlite file is self-contained

One mbtiles/pois.sqlite/routes.sqlite/walk.sqlite per source PBF. The server
globs them; the client gets merged results. Adding a new country is literally
dropping a `.osm.pbf` in `osmpbf/` and re-running `make-tiles.sh`. No
central index, no migration step.

### Client storage follows the same boundaries

Per-region IDB records for tiles/POIs/routes/walk-graph. Deleting a region
deletes those records cleanly; other regions unaffected. Favorites are a
global concept so they live in their own store.

### Naming conventions

- URL paths match the asset type: `/tiles`, `/poi`, `/routes`, `/walk-graph`,
  `/fonts`, `/icons`. The SW matches path prefixes.
- IDB database names: `cc.<asset>.v<N>`. Versioning is explicit — renaming
  `cc.routes.v1` to `cc.routes.v2` forces fresh data on schema change.
- localStorage keys: `cc.<feature>` or `cc.<feature>.v<N>`.

### Degrade gracefully, don't fall back to network

If an icon isn't cached, `styleimagemissing` fires, load attempt returns 204,
MapLibre shows nothing — coalesce falls back to `poi-default`. No silent
network fetch to "make it work".

If a region's POIs aren't cached, search returns empty — not a server query
with a spinner.

If the walk graph doesn't extend to the destination, A* returns null and the
UI says "no route found" — not a fallback to Google.

## Memory keys / state surfaces

Where the user's data lives, at a glance:

| State | Location | Scope |
|---|---|---|
| Tiles | Cache API (`tiles-v1`) | Per-URL, durable |
| Fonts | Cache API (`app-v1`) | Shared, durable |
| Icons (SVG files) | Cache API (`app-v1`) | Shared, durable |
| Icons (registered MapLibre images) | Map runtime | Per-session |
| App shell | Cache API (`app-v1`) | Durable until refresh |
| POIs | IndexedDB `cc.pois.v1` | Per-region |
| Routes (features) | IndexedDB `cc.routes.v2/routes` | Per-region |
| Routes (metadata) | IndexedDB `cc.routes.v2/meta` | Shared |
| Walking graph | IndexedDB `cc.walk.v1` | Per-region |
| Favorites | IndexedDB `cc.favorites.v1` | Global |
| Region list | localStorage `cc.regions.v1` | Global |
| Last view | localStorage `cc.view.v1` | Session persistence |
| Selected POI | localStorage `cc.selected.v1` | Persistent across reloads |
| Theme + custom colors | localStorage `cc.theme`, `cc.themeCustom` | Global |
| Units | localStorage `cc.units` | Global |
| Transit state | localStorage `cc.transit.v2` | Global |

## Gotchas discovered (and solved)

- **pyosmium** in nixpkgs is `python3Packages.pyosmium`, not `python3Packages.osmium`.
  The module imports as `osmium` though.
- **MapLibre custom markers** get `position: absolute` set by MapLibre;
  adding `position: relative` to the marker element overrides that and the
  marker stops tracking the coord. Children can be absolute-positioned
  without needing an extra `position: relative`.
- **`-R` flag on `osmium tags-filter`** means *omit referenced objects* (it's
  inverted from what you'd expect). Default behavior already includes
  referenced ways/nodes.
- **`styleimagemissing`** fires before `loadAllIcons` completes on fresh
  visit, so the fallback handler tried to register the default marker
  under real-icon names — stomping the real icons when they finally
  loaded. Fix: fallback handler only tries `loadIcon(id)`, never registers
  the default under the real ID; coalesce in the style handles the missing
  case naturally.
- **First-visit controller race** — `navigator.serviceWorker.ready` resolves
  when the SW activates but the page may not yet be controlled. Needed
  explicit `controllerchange` await before creating the map, or else
  MapLibre's first tile fetches slip past the SW.
- **Multi-country tile serving at low zoom** — both Canada and US
  mbtiles have z0-z5 world tiles with different data. Alphabetical
  first-match was wrong. Fixed by picking the tile with the largest body
  (proxy for "this one has the relevant country's features").
- **MapLibre class vs subclass for transit tile data** — tilemaker's
  `process-openmaptiles.lua` puts railway kind in `class`, not `subclass`.
  My filters used the wrong field initially, resulting in no transit
  tracks rendering.

## What could still be improved

### Known rough edges

- **Icon registration lag on initial load / theme change** — 215 SVG → canvas
  → ImageData operations take time. Attempted lazy-loading via
  `styleimagemissing` in this session, backed out because it broke
  something (revisit with better repro).
- **Route-relation line collisions** — if 5 bus routes share a street
  segment, the rendered line shows only one color (first in the
  route_ids list). Drawing offset parallel lines per-route would be
  much nicer but requires geometric line-offset math.
- **Walk graph first-tile download is slow** — even with gzip + temp-table
  joins, city-sized walk graphs are 5-20 MB; initial fetch + parse + IDB
  write is multiple seconds. Two ideas worth trying:
  - **Graph simplification at build time** — merge degree-2 shape-point
    nodes into polylines. Typical 3–5× reduction in node/edge count.
    Would require a post-processing step in build-walk-graph.py.
    Estimated 80 lines of Python.
  - **Custom binary format** instead of JSON — varint node-IDs,
    delta-encoded coords, dictionary-compressed names. Would give maybe
    2–3× more compression on top of gzip.
- **Transit route filtering UX** — route list is currently flat
  alphabetical. For 200+ routes, grouping by network/operator with
  collapsible sections would help.
- **Popup for overlapping route lines** is currently 20-pixel bbox hit
  test. Might still miss on zoom levels where lines are very thin —
  could scale the bbox with inverse zoom.

### Not built, but would be natural additions

- **Transit stops/stations/platforms** — click a bus stop POI to see
  which routes serve it. Design is sketched; requires extending
  `build-routes-db.py` to also capture node members of route
  relations and their roles, plus a new IDB store and proximity match
  in the POI click handler.
- **Multi-modal routing (walk + transit)** — OTP-style. Needs GTFS
  feeds per transit agency + time-dependent graph + RAPTOR. Large
  undertaking, 5+ sessions. Current walking-only routing is the 80%
  case for the stamp-game use case.
- **Real turn-by-turn navigation** — current directions panel is a
  list of steps without distance-remaining or active highlighting.
  Could snap user location to the route line and show "in X m, turn
  right on Y".
- **Custom icon upload** — user-provided SVG for specific favorite types.
- **Favorite categories / folders** — grouping, tags, notes.
- **Share favorites / regions** via a URL that embeds a JSON payload
  (or a link to a pre-exported JSON file).
- **Custom routing profiles** — walking, cycling, wheelchair-accessible
  paths, avoiding stairs.

### Infrastructure / code health

- **`index.html` is ~2500 lines** and growing. Could start splitting
  into modules if the no-build-step constraint can be relaxed
  (e.g., native ES module imports).
- **Error handling is inconsistent** — some async paths have explicit
  try/catch + user-visible fallback, others silently swallow.
- **No automated tests.** Syntax check via `node --check` and manual
  testing on device. For a hobby PWA this is fine; for production a
  few Playwright scripts on the key flows (save region, search, route)
  would catch regressions.
- **Memory pressure when loading large walk graphs** — a multi-city
  save can build an in-memory graph of millions of edges. Tested OK
  for Canada-scale so far but there's a ceiling.
- **Flask is single-threaded by default** — concurrent saves from
  multiple clients would queue. Not an issue for single-user personal
  deploy but worth noting.
- **No compression on POI/routes responses yet** — only walk-graph
  is gzipped. Would be cheap to apply `gzip_json()` wrapper to the
  others too.

## Deployment checklist (for the record)

1. `direnv allow` to load the nix shell (python3 + flask + shapely +
   pyosmium + tilemaker + osmium-tool + cloudflared + git)
2. Drop `.osm.pbf` files into `osmpbf/` (e.g., from Geofabrik)
3. `./get-fonts.sh` — clones KlokanTech fonts into `fonts/`
4. `./get-icons.sh` — clones Maki icons into `icons/`
5. `./make-tiles.sh` — builds mbtiles + pois.sqlite + routes.sqlite +
   walk.sqlite for each PBF (can take hours for country-scale)
6. `python run.py` — server on :8465
7. Cloudflare tunnel to `poke.phylliidaassets.org`
8. On phone: visit the tunnel URL, Add to Home Screen, tap
   `↓ app data` + `↓ save current view` over wifi, then disconnect

## Credits

- OSM + Geofabrik for source data
- tilemaker + OpenMapTiles schema for tile generation
- MapLibre GL JS for client rendering
- Maki icons (Mapbox, MIT)
- KlokanTech Noto Sans glyphs
- pyosmium + shapely for build-time geometry work
