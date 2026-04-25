# Session 2 — Transit, Multi-modal Routing, and Compression

Continuation of `SESSION_SUMMARY.md`. The first session built the PWA shell, offline tiles, POIs, walk graph, favorites, pin-drop, themes, and OSM-relation-based transit routes/stops. This session layered a full multi-modal trip planner on top of GTFS, plus a pile of space and speed optimizations.

---

## What got built (rough chronological)

### OSM route stops + popup UX
- Captured stop/platform node members of OSM route relations in `build-routes-db.py` with order + role.
- Server `/routes` returns `stops` map alongside routes.
- Client indexes stops by OSM node_id for proximity lookup (100m grid) on map-click.
- Unified popup: clicking a route line OR a transit-infra POI (`stop_position`, `platform`, `station`) surfaces a combined list of routes serving that area, each with:
  - ✓ toggle visibility (line on/off)
  - 👁 view stops (bubbles along the route's stops)
- Transit menu: per-mode chips now actually toggle all routes of that mode. "Active only" filter checkbox added.

### GTFS schedules — ingestion pipeline
- `get-gtfs-stm.sh` — downloads STM zip
- `get-gtfs-catalog.py` — filters Mobility Database `feeds_v2.csv` by ISO country code → tab-separated feed list
- `validate-gtfs.py` — 9 assumption checks (required files, sorted stop_times, referential integrity, etc). Exit code 2 on errors, 1 on warnings, 0 clean
- `ingest-gtfs.py` — streaming orchestrator: download one zip → validate → ingest → delete → next. Resumable via `feed_meta` table. Per-feed failure isolation
- `build-schedule-db.py` — the heart of the pipeline. Evolved through three generations:
  - **Gen 1**: naive stop_times table → 594 MB for STM
  - **Gen 2**: pattern normalization (dedupe trips into unique stop sequences + per-trip varint time deltas) → 30 MB
  - **Gen 3**: integer-interned FKs for service_id, headsign, timings; dropped TEXT trip.id → **10 MB**
  - **Gen 4 (this session's last)**: added GTFS `shapes.txt` ingestion. Shape is zigzag-varint-delta blob; trip gets `shape_num` FK. ~+1 MB for STM
- `link-gtfs-to-osm.py` — fuzzy matches GTFS stops to OSM route_stop nodes by proximity (30m) + fuzzy name score. Writes `gtfs_osm_link` table in schedule.sqlite

**Current full-Canada + STM + US numbers (reference)**:
- STM alone: 12 MB schedule.sqlite (with shapes)
- 107 Canada feeds + STM: ~80 MB
- +902 US feeds on top: ~400 MB total
- Typical gzipped `/schedule?bbox=` response for one city: 100 KB – 3 MB

### Multi-modal trip planner
Time-dependent Dijkstra over a combined walk-graph + transit graph.

**Data structures built at client load (`rebuildScheduleIndex`)**:
- `osmToGtfs` — OSM node → GTFS stop IDs (from `gtfs_osm_link`)
- `stopPatterns` — GTFS stop → list of [pattern_id, seq] pairs
- `patternBboxStops` — pattern → sorted [[seq, stop_id], …] of in-bbox stops
- `shapeByPattern` — pattern → decoded GTFS shape polyline (for transit line rendering)
- `gtfsRouteByRefMode` — `"bus:105"` style key → GTFS route IDs (for OSM↔GTFS route bridging when drawing bus lines on the map)
- `stopToWalkNode` / `walkNodeToStops` — bridges built by `buildMultimodalBridges()` via 100m grid, 500m radius. Called at end of both `rebuildScheduleIndex` and `rebuildWalkGraph` so ordering between them doesn't matter.

**Algorithm details**:
- Nodes are either `w:<walk_id>` or `s:<gtfs_stop_id>`.
- **Cost-based Dijkstra** (not time-based) so `walkWeight` (user-configurable multiplier on walking time) affects route choice. Transit ride time and wait time both count at 1×.
- PQ entries carry `[cost, key, wallTime]`. The wall-clock time is needed for schedule lookups (boarding a trip requires `t ≥ departure - buffer`).
- **Cross-midnight handling**: trips are tagged with day-offset (`0` / `-86400` / `+86400`) depending on whether they run today's / yesterday's / tomorrow's service. Effective time = raw + offset.
- Iterated alternatives: after a result is found, shift the time pivot past the first transit boarding (depart-mode) or the last transit alighting (arrive-mode) and re-run. Up to 5 alternatives, sorted by user's weighted cost.
- **Force-transit fallback**: if the primary result has no transit, runs a second pass with `walkWeight = 99` to surface transit alternatives anyway.
- **Reverse Dijkstra for arrive-by**: min-cost search backward from destination with MinHeap on −time priority effectively; upstream stops relaxed via "latest trip with arrival ≤ current time", then `cost += (bestAr − dep)` and time updated to `dep − transferSec`.

**Subtle-but-important bug fixed at the end**:
- `t − t0 > MAX_SEARCH_SECS` check used `break` (from time-based Dijkstra era). In cost-based Dijkstra, pops aren't time-ordered — a cheap-cost-but-long-wall-clock path can surface early and prematurely terminate the whole search. Fixed by changing to `continue` (skip the single entry, keep searching).

### Routing UI
- New dedicated `#routingPanel` top overlay: From/To inputs with typeahead dropdowns (My Location + favorites + POIs), swap button, when-mode (Leave now / Depart at / Arrive by) with `datetime-local`, walk-cost multiplier, transfer-min, Plan button. Saves/restores from localStorage.
- `#directionsPanel` bottom overlay renders the result: option strip ("pills") per alternative, map polyline in blue-walk + transit-colored segments, intermediate stop dots, step-by-step instructions with stop counts.
- **Save route** (💾) serializes the rendered snapshot (styled coord segments + step data + intermediate stop features) to localStorage. **View saved** opens a sheet. **Minimize** (▼) collapses the panel to just the summary so the map is visible.
- When a saved route is displayed, the 💾 button hides (already saved).
- Delete-saved confirms.

### Map rendering
- `walk-route` source: styled features with `kind: walk|transit` + optional color. Two layers filter by `kind`:
  - `walk-route` (dashed blue) for walking legs
  - `walk-route-transit` (colored by feature.colour) for transit legs
- New `route-trip-stops` source + circle layer for per-leg intermediate stop bubbles (populated from `patternBboxStops` filtered to the leg's seq range, plus boarding + alighting).
- **bus-routes source** (the transit-toggle overlay) now prefers GTFS shape polylines via `(mode, ref)` matching to the OSM routes in the toggle UI, with OSM route-data as fallback.

### Settings additions
- Clock format (24h / 12h AM-PM), persisted as `cc.clockFormat`.
- Time zone dropdown (IANA names via `Intl.supportedValuesOf`), persisted as `cc.timezone`. All time math — `todayInfo`, `fmtClockTime`, `datetime-local` parse/format — routes through the configured tz.

### Walk-graph optimization (separate big refactor)
- `build-walk-graph.py` rewritten: pass 1 identifies intersection nodes (endpoints + ≥2-way shared); pass 2 emits polyline edges between intersections with shape points packed as zigzag-varint-delta blobs, names interned.
- Schema: `walk_node(id, osm_id, lng_u, lat_u)` (microdegree ints), `walk_edge(from, to, weight_m, name_id, shape_blob)`, `walk_name(id, text)`.
- Canada: **7.4 GB → 1.1 GB**. US: 40+ GB → 12 GB. Inserts batched + wrapped in transaction.
- Server `/walk-graph?bbox=` detects new vs legacy schema per file.
- Client decodes shape blobs for pretty route rendering (follows actual road curves).

### README & docs
- Added "Transit schedules (GTFS)" section to `README.md` covering: quick STM test, full Canada + US workflow using Mobility Database catalog, resumability notes, size estimates.

---

## Design philosophy (continued from session 1)

**Offline-first stays non-negotiable.** All planning and rendering happens client-side from IDB-persisted data. The `/schedule?bbox=` endpoint is only hit via the "save region" button.

**Favor "correct" over "fast but wrong."** We repeatedly chose the more accurate representation when the cost was reasonable:
- Pattern-normalized schedule over per-trip stop_times
- GTFS `shapes.txt` over walking-A* approximation for transit polylines
- Reverse-time Dijkstra for arrive-by instead of pretending forward would do

**Compress aggressively, but through structure, not algorithms.** The schedule DB went from 594 MB → 10 MB not through gzip, but by recognizing that trips share stop sequences (patterns), most timing variation clusters into a few unique blobs per pattern, and TEXT IDs are expensive to duplicate. Same for walk graph (84.5% shape-point collapse).

**User-visible flags over hard-coded constants.** `walkWeight`, `transferSec`, clock format, and timezone are all user-configurable. Defaults are sensible but not a ceiling.

**Don't hide that things are approximations.** When the fallback for transit polyline is straight-lines-through-stops rather than A*-through-walk-graph, we accepted a slightly uglier visual for enormous speed gain — 10 seconds per swap → instant.

---

## Known issues / caveats / things to watch

- **Arrive-by approximates wait time as `transferSec`** in the reverse Dijkstra. In the forward planner, `wait_sec = dep - t_arrival_at_stop` is the actual wait. Asymmetric by up to the buffer amount; invisible in practice.
- **Shape clipping uses nearest-vertex projection, not perpendicular-segment projection.** If shape vertices are sparse (e.g., metro with long straight runs), the clip might be a few meters off from the real stop. Usually fine visually.
- **GTFS↔OSM route matching is heuristic** (`mode:ref` key). If multiple agencies share a route number like "1" (bus/tram/metro conflicts), the first match wins. Could improve by also matching on agency_id + network.
- **Cost-Dijkstra with wait-is-free-but-stops-advance-time** has a pareto-optimality gap: an earlier-time-but-higher-cost path can be discarded when a later-time-cheaper path to the same node is found, potentially missing a downstream trip that only the earlier state could catch. Rare in practice.
- **Saved routes embed rendered geometry + step data only** (not the inputs needed to re-plan). "Re-run this plan" would require storing fromEp/toEp/whenMode/whenDt/walkWeight/transferSec. Intentionally deferred.
- **`MAX_SEARCH_SECS` (3 hours)** is a hard cap. For cross-country train routes (> 3h), the search terminates prematurely. Bump if needed.
- **Cross-midnight service_id handling uses `setDate(±1)`** which can skew by ±1 hour around DST transitions. Practically negligible.
- **Walk-weight force-transit backup uses `walkWeight=99`** — always finds a transit route if geometrically possible, but the route might be absurd (e.g., a 2-min walk becomes a 30-min transit ride). The sort-by-user-weight-then-display means the absurd backup stays at the bottom of the list, though.

---

## File map — what's where

| File | Role |
|---|---|
| `run.py` | Flask server, all `/routes` `/poi` `/walk-graph` `/schedule` `/tiles` endpoints |
| `static/index.html` | Everything client-side (no build step). ~4700 lines. |
| `build-poi-db.py` | OSM → POI sqlite with address enrichment |
| `build-routes-db.py` | OSM relations → routes + stops sqlite |
| `build-walk-graph.py` | OSM highways → compact walk graph sqlite |
| `build-schedule-db.py` | GTFS zip → schedule sqlite (patterns + timings + shapes) |
| `get-gtfs-catalog.py` | Filter Mobility Database catalog by country |
| `get-gtfs-stm.sh` | Download STM zip |
| `validate-gtfs.py` | Pre-ingest checks on a GTFS zip |
| `ingest-gtfs.py` | Streaming multi-feed orchestrator |
| `link-gtfs-to-osm.py` | Fuzzy stop matcher |
| `make-tiles.sh` | Wraps it all for the "just run this" case |
| `README.md` | User-facing setup / run / tunnel docs |
| `SESSION_SUMMARY.md` | First-session notes |

**Data & logs** (gitignored or should be):
- `data/*.mbtiles` — vector tiles per country
- `data/*.pois.sqlite` — per-country POI index
- `data/*.routes.sqlite` — per-country OSM route relations + stops
- `data/*.walk.sqlite` — per-country walk graph
- `data/schedule.sqlite` — unified GTFS (all agencies mingled; feed_meta tracks origin)
- `data/mdb-catalog.csv` — Mobility Database catalog
- `feeds-*.tsv` — per-country feed lists
- `osmpbf/*.osm.pbf` — Geofabrik extracts
- `gtfs/*.zip` — cached raw GTFS downloads (transient)
- `logs/*.log` — ingest run output

---

## Client-side state surfaces

IndexedDB:
- `cc.pois.v1` — per-region POI blobs
- `cc.routes.v2` — per-region OSM routes + meta
- `cc.walk.v1` — per-region walk graph
- `cc.schedule.v1` — per-region GTFS schedule payload (stops, patterns, trips, shapes, services, …)
- `cc.favorites.v1` — favorites global

localStorage:
- `cc.regions.v1` — list of saved regions (name, bbox, url set)
- `cc.view.v1` — last map view
- `cc.selected.v1` — currently-selected POI
- `cc.theme`, `cc.themeCustom` — UI theme
- `cc.units` — m / mi
- `cc.clockFormat` — 24h / 12h
- `cc.timezone` — IANA tz
- `cc.walkWeight`, `cc.transferMin` — router inputs
- `cc.transit.v2` — per-mode and per-route visibility
- `cc.savedRoutes.v1` — saved trip plans

---

## Future plans / natural next steps

**Short horizon**:
- **Re-plan from saved route** — store inputs alongside snapshot so user can tap "re-plan" to get the latest same-day alternatives.
- **Filter by service day** — let the user pick "route for a Sunday at 3pm" explicitly vs. inferring from the `datetime-local`.
- **Accessibility info in trip plans** — GTFS has `wheelchair_accessible` flags on trips and `wheelchair_boarding` on stops. Propagate + filter.
- **Agency attribution in the UI** — tiny "Data: STM, STL, RTL…" footer in relevant regions.
- **Stop-click popup improvements** — show "next bus here in 5 min" for stops even when no route is planned, using the existing `nextDeparturesForOsmNodes`.

**Medium horizon**:
- **Frequency-based trip compression** — some agencies publish GTFS `frequencies.txt` with headway definitions instead of enumerating every trip. Parse it and you could drop thousands of trip rows.
- **Real-time feeds (GTFS-RT)** — would compromise the strict offline-first invariant. Only acceptable if explicitly opt-in via a button.
- **Transfer-rule support** — GTFS `transfers.txt` has min-transfer-time constraints per (from_stop, to_stop) pair. Currently we use one flat `transferSec`.
- **Wheelchair / bike-friendly walk routing** — the walk graph already carries way names; tagging + filtering for accessibility profiles is incremental.
- **Multi-agency shape coherence** — some routes have multiple agencies covering the same line (Montreal↔Laval metro). Unify visually.
- **Export route as geojson / share link** — "here's my route to X" encoded in a URL.

**Long horizon / speculative**:
- **Pre-computed transfer shortcuts (Contraction Hierarchies)** — Dijkstra is fine for within-city but becomes slow for cross-region planning. CH or RAPTOR-style speedups could make national-scale plans fast.
- **Vehicle realtime positions (GTFS-RT vehicle updates)** — "the bus is here now". Breaks offline but is useful.
- **Bike + multimodal (bike-to-transit, scooter, carshare)** — framework already supports it; would need extra graph layers.
- **Server-side trip planning** — current design is client-side-only for offline. A server endpoint could help thin clients, but goes against the core architectural goal.

---

## Quick-start for continuing development

```bash
# Get into the nix shell
direnv allow

# One-time data setup
./get-fonts.sh && ./get-icons.sh
# Drop .osm.pbf files into osmpbf/
./make-tiles.sh     # generates mbtiles + POIs + routes + walk graph
./get-gtfs-stm.sh   # downloads STM GTFS
python3 build-schedule-db.py stm gtfs/stm.zip data/schedule.sqlite
python3 link-gtfs-to-osm.py data/schedule.sqlite data/canada-260417.routes.sqlite

# Run
python3 run.py                    # port 8465
cloudflared tunnel --url http://localhost:8465   # expose via HTTPS

# On phone: open the tunnel URL, Add to Home Screen, tap
# "↓ app data" + "↓ save current view" over wifi, then disconnect
```

**For a specific hotfix cycle**: edit `static/index.html`, reload page, test. No build step. Always run `node -e "..."` syntax check after edits (pattern used throughout the session).

**For schema changes** to schedule/walk/routes: either bump version number in the DB name (client IDB store) or write a migration. Most refactors in this session just bumped the format and required a re-save.

---

## Closing notes

The thing that still most impresses me about this codebase is that it's **one HTML file + a handful of Python scripts** and yet ends up with:
- Full-city vector tile rendering
- Offline POI search with address enrichment
- Multi-modal trip planner with real GTFS schedules, alternatives, and user-tunable preferences
- Authoritative vehicle polylines
- Saves-for-later
- All of continental North America's transit data in a ~400 MB file

The choice to stay offline-first and client-rendered forces simpler architecture than a "real" app would have, and nearly every optimization compounds — because the client has to hold the data anyway, compressing it helps both download time AND client memory AND disk usage.

Good luck continuing this. The big thing I'd flag for a next session: if schedule data starts getting stale (agencies update weekly), re-ingesting needs to be more selective than "wipe and re-run". Adding a "refresh feed" command to `ingest-gtfs.py` that wipes just one feed's rows and re-ingests is the smallest useful add.
