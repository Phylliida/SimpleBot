import array
import base64
import contextlib
import gzip
import json
import pathlib
import sqlite3
import struct
import sys
import time
from io import BytesIO
from flask import Flask, g, send_from_directory, Response, abort, request, jsonify

# array.array native int widths are platform-dependent in theory; all mainstream
# server platforms have 4-byte int / unsigned int. Fail fast if ever not true.
assert array.array('I').itemsize == 4, "unexpected native unsigned-int width"
assert array.array('i').itemsize == 4, "unexpected native int width"


def _le_bytes(values, typecode):
    a = array.array(typecode, values)
    if sys.byteorder == 'big':
        a.byteswap()
    return a.tobytes()


def gzip_json(data):
    body = gzip.compress(
        json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        compresslevel=5,
    )
    resp = Response(body, mimetype="application/json")
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Vary"] = "Accept-Encoding"
    return resp


# Overlap checks use the rtree's native spatial index (O(log n)) instead of
# a MIN/MAX scan that would read the entire index on cold starts. Each file's
# open() connection is cheap; the rtree lookup returns in ~ms even on huge
# countries.
def _rtree_overlaps(path, table, w, s, e, n):
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                f"SELECT 1 FROM {table} "
                f"WHERE minX <= ? AND maxX >= ? AND minY <= ? AND maxY >= ? "
                f"LIMIT 1",
                (e, w, n, s),
            ).fetchone()
        return row is not None
    except sqlite3.DatabaseError:
        return False


def _poi_overlaps(path, w, s, e, n):
    # Prefer the rtree if it's present (built via add-poi-rtree.py); fall
    # back to the flat lat/lng indexes for DBs that haven't been migrated.
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            has_rtree = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='poi_rtree'"
            ).fetchone()
            if has_rtree:
                row = conn.execute(
                    "SELECT 1 FROM poi_rtree "
                    "WHERE minX <= ? AND maxX >= ? AND minY <= ? AND maxY >= ? "
                    "LIMIT 1",
                    (e, w, n, s),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM poi "
                    "WHERE lng BETWEEN ? AND ? AND lat BETWEEN ? AND ? LIMIT 1",
                    (w, e, s, n),
                ).fetchone()
        return row is not None
    except sqlite3.DatabaseError:
        return False


def _relevant_files(glob_pattern, overlap_fn, w, s, e, n):
    if not DATA_DIR.exists():
        return []
    out = []
    for path in sorted(DATA_DIR.glob(glob_pattern)):
        if overlap_fn(path, w, s, e, n):
            out.append(path)
    return out

ROOT = pathlib.Path(__file__).parent
DATA_DIR = ROOT / "data"
SCHEDULE_PATH = DATA_DIR / "schedule.sqlite"

app = Flask(__name__, static_folder="static")


@contextlib.contextmanager
def _phase(name):
    """Record wall-clock duration of a code block under `g.phases[name]`.
    Works only during a Flask request (no-op if `g` has no phases dict)."""
    start = time.perf_counter()
    try:
        yield
    finally:
        try:
            g.phases[name] = (time.perf_counter() - start) * 1000.0
        except (AttributeError, RuntimeError):
            pass


@app.before_request
def _download_timing_start():
    g.t0 = time.perf_counter()
    g.phases = {}  # name -> ms
    g.meta = {}    # name -> scalar (counts, etc.)


@app.after_request
def _no_http_cache_for_js(resp):
    # Tell Safari not to HTTP-cache our own JS. The service-worker Cache API
    # already holds a canonical copy, so the browser HTTP cache is pure
    # duplicate — and on iOS Safari each hard refresh retains the old entry
    # alongside the new one, growing storage.estimate() ~1 MB per refresh.
    if (request.path.startswith("/static/")
        and (request.path.endswith(".js") or request.path.endswith(".css"))):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.after_request
def _download_timing_log(resp):
    # Only log requests initiated by a download flow (client adds this header
    # for POI/schedule/walk/tile fetches triggered by "save current view").
    if request.headers.get("X-Download") != "1":
        return resp
    elapsed_ms = (time.perf_counter() - g.t0) * 1000.0
    size = resp.calculate_content_length()
    size_str = f"{size / 1024:.1f}KB" if size is not None else "?KB"
    phases = getattr(g, "phases", {})
    meta = getattr(g, "meta", {})
    phase_str = (" " + " ".join(f"{k}={v:.0f}ms" for k, v in phases.items())) if phases else ""
    meta_str = (" " + " ".join(f"{k}={v}" for k, v in meta.items())) if meta else ""
    qs = ("?" + request.query_string.decode("ascii", "replace")) if request.query_string else ""
    if len(qs) > 80:
        qs = qs[:77] + "..."
    print(f"[dl] {request.method} {request.path}{qs} "
          f"size={size_str} total={elapsed_ms:.0f}ms{phase_str}{meta_str}", flush=True)
    return resp


@app.route("/")
def index():
    resp = send_from_directory("static", "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/sw.js")
def sw():
    resp = send_from_directory("static", "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory("static", "manifest.webmanifest")


@app.route("/fonts/<fontstack>/<filename>")
def fonts(fontstack, filename):
    path = ROOT / "fonts" / fontstack / filename
    if not path.is_file():
        abort(404)
    resp = send_from_directory(path.parent, path.name, mimetype="application/x-protobuf")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/fontslist/<fontstack>")
def fonts_list(fontstack):
    d = ROOT / "fonts" / fontstack
    if not d.is_dir():
        abort(404)
    return {"files": sorted(f.name for f in d.iterdir() if f.name.endswith(".pbf"))}


@app.route("/icons/<name>")
def icons(name):
    path = ROOT / "icons" / name
    if not path.is_file():
        abort(404)
    resp = send_from_directory(path.parent, path.name, mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/iconslist")
def icons_list():
    d = ROOT / "icons"
    if not d.is_dir():
        abort(404)
    return {"files": sorted(f.name for f in d.iterdir() if f.name.endswith(".svg"))}


@app.route("/creature-names")
def creature_names():
    # Plain-text "one species name per line", 1-indexed (line 1 == pokemon 1).
    # Returned as a JSON array so the client can store/index it trivially.
    path = ROOT / "data" / "Battlers" / "pokemon.txt"
    if not path.is_file():
        abort(404)
    names = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    resp = jsonify(names)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/creature-sprite/<int:sheet>")
def creature_sprite(sheet):
    # Each sheet is a 960×4896 PNG holding 10×51 fusion sprites at 96×96.
    # The client fetches a sheet the first time any creature in that
    # fusion "partner B" family is shown, crops the needed index, caches
    # the individual sprite in IndexedDB, and drops the sheet.
    path = ROOT / "data" / "Battlers" / "spritesheets_autogen" / f"{sheet}.png"
    if not path.is_file():
        abort(404)
    resp = send_from_directory(path.parent, path.name, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# Field codes for binary POI props. Keys must stay in lock-step with the
# client's POI_FIELDS array — reordering here breaks existing IDB data.
POI_FIELDS = [
    "addr:housenumber", "addr:street", "addr:city", "addr:county",
    "addr:state", "addr:country", "addr:postcode", "opening_hours",
    "phone", "contact:phone", "website", "contact:website",
    "wheelchair", "brand", "operator", "cuisine", "description",
    "wikipedia", "wikidata", "internet_access",
]
POI_FIELD_CODE = {k: i for i, k in enumerate(POI_FIELDS)}


@app.route("/poi")
def poi():
    """Binary POI bundle. Layout (little-endian, 4-byte aligned):
        Header (32 bytes):
            0:  'POIB' magic
            4:  u32 version (=1)
            8:  u32 N (poi count)
            12: u32 S (string pool count)
            16: u32 stringsByteLen
            20: u32 propsByteLen
            24: u32 reserved (0)
            28: u32 reserved (0)
        Columns (20N):
            N × f32 lng
            N × f32 lat
            N × i32 name_idx      (-1 = none; else idx into string pool)
            N × i32 category_idx  (-1 = none)
            N × u32 props_off     (0xFFFFFFFF = none; else byte offset into props block)
        Strings (stringsByteLen): S × (u16 utf8_len + utf8 bytes)
        Props  (propsByteLen):    per-POI record at props_off:
            u8 field_count
            field_count × (u8 field_code, u32 string_idx)
    """
    try:
        parts = [float(x) for x in request.args.get("bbox", "").split(",")]
    except ValueError:
        abort(400)
    if len(parts) != 4:
        abort(400)
    w, s, e, n = parts

    string_pool = []
    string_to_idx = {}

    def intern(text):
        if not text:
            return -1
        idx = string_to_idx.get(text)
        if idx is None:
            idx = len(string_pool)
            string_to_idx[text] = idx
            string_pool.append(text)
        return idx

    lngs = []
    lats = []
    name_idx = []
    category_idx = []
    props_offs = []
    props_buf = BytesIO()

    for path in _relevant_files("*.pois.sqlite", _poi_overlaps, w, s, e, n):
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            has_rtree = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='poi_rtree'"
            ).fetchone()
            if has_rtree:
                rows = conn.execute(
                    "SELECT poi.lng, poi.lat, poi.name, poi.category, poi.props "
                    "FROM poi_rtree JOIN poi ON poi.rowid = poi_rtree.id "
                    "WHERE poi_rtree.minX <= ? AND poi_rtree.maxX >= ? "
                    "  AND poi_rtree.minY <= ? AND poi_rtree.maxY >= ?",
                    (e, w, n, s),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT lng, lat, name, category, props FROM poi "
                    "WHERE lng BETWEEN ? AND ? AND lat BETWEEN ? AND ?",
                    (w, e, s, n),
                ).fetchall()
            for lng, lat, name, category, props_json in rows:
                lngs.append(lng)
                lats.append(lat)
                name_idx.append(intern(name) if name else -1)
                category_idx.append(intern(category) if category else -1)

                props = None
                if props_json:
                    try:
                        props = json.loads(props_json)
                    except json.JSONDecodeError:
                        props = None

                emitted = []
                if props:
                    for k, v in props.items():
                        code = POI_FIELD_CODE.get(k)
                        if code is None or not v:
                            continue
                        emitted.append((code, intern(str(v))))

                if emitted:
                    props_offs.append(props_buf.tell())
                    props_buf.write(struct.pack("<B", min(len(emitted), 255)))
                    for code, sidx in emitted[:255]:
                        props_buf.write(struct.pack("<BI", code, sidx))
                else:
                    props_offs.append(0xFFFFFFFF)

    N = len(lngs)
    S = len(string_pool)

    strings_buf = BytesIO()
    for s_str in string_pool:
        b = s_str.encode("utf-8")
        if len(b) > 65535:
            b = b[:65535]
        strings_buf.write(struct.pack("<H", len(b)))
        strings_buf.write(b)
    strings_bytes = strings_buf.getvalue()
    props_bytes = props_buf.getvalue()

    body = b"".join([
        struct.pack("<4sIIIIIII",
                    b"POIB", 1, N, S,
                    len(strings_bytes), len(props_bytes), 0, 0),
        _le_bytes(lngs, "f"),
        _le_bytes(lats, "f"),
        _le_bytes(name_idx, "i"),
        _le_bytes(category_idx, "i"),
        _le_bytes(props_offs, "I"),
        strings_bytes,
        props_bytes,
    ])

    compressed = gzip.compress(body, compresslevel=1)
    resp = Response(compressed, mimetype="application/octet-stream")
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Vary"] = "Accept-Encoding"
    g.meta["N"] = N
    g.meta["S"] = S
    return resp


@app.route("/housenumbers")
def housenumbers():
    """Binary housenumber bundle. Layout (little-endian):
        Header (40 B):
            0:  'HSNB' magic
            4:  u32 version = 1
            8:  u32 N (count)
            12: u32 M (unique strings)
            16: u32 stringsByteLen
            20: u32 reserved (0)
            24: f32 bbox west  (coords are quantised into bbox on server,
            28: f32 bbox south  and reconstructed by the client — ~1 m
            32: f32 bbox east   resolution over a city-sized bbox at u16)
            36: f32 bbox north
        Columns (6 * N):
            N × u16 lng_q
            N × u16 lat_q
            N × u16 str_idx
        Strings (stringsByteLen):
            M × (u16 utf8_len + utf8 bytes)
    """
    try:
        parts = [float(x) for x in request.args.get("bbox", "").split(",")]
    except ValueError:
        abort(400)
    if len(parts) != 4:
        abort(400)
    w, s, e, n = parts

    string_pool = []
    str_to_idx = {}
    lngs = []
    lats = []
    str_indices = []

    for path in _relevant_files("*.housenumbers.sqlite",
                                 lambda p, *a: _rtree_overlaps(p, "hn_rtree", *a),
                                 w, s, e, n):
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT h.lng_u, h.lat_u, h.text FROM hn h "
                "JOIN hn_rtree r ON h.id = r.id "
                "WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?",
                (e, w, n, s),
            ).fetchall()
            for lng_u, lat_u, text in rows:
                if not text:
                    continue
                idx = str_to_idx.get(text)
                if idx is None:
                    idx = len(string_pool)
                    str_to_idx[text] = idx
                    string_pool.append(text)
                lngs.append(lng_u / 1e6)
                lats.append(lat_u / 1e6)
                str_indices.append(idx)

    N = len(lngs)
    M = len(string_pool)

    if N == 0:
        bw, bs, be, bn = 0.0, 0.0, 0.0, 0.0
    else:
        bw, bs, be, bn = min(lngs), min(lats), max(lngs), max(lats)
    lng_span = max(be - bw, 1e-9)
    lat_span = max(bn - bs, 1e-9)

    lngs_q = [max(0, min(65535, round((v - bw) / lng_span * 65535))) for v in lngs]
    lats_q = [max(0, min(65535, round((v - bs) / lat_span * 65535))) for v in lats]

    names_buf = BytesIO()
    for text in string_pool:
        b = text.encode("utf-8")
        if len(b) > 65535:
            b = b[:65535]
        names_buf.write(struct.pack("<H", len(b)))
        names_buf.write(b)
    names_bytes = names_buf.getvalue()

    header = struct.pack(
        "<4sIIIIIffff",
        b"HSNB", 1, N, M, len(names_bytes), 0,
        float(bw), float(bs), float(be), float(bn),
    )

    body = b"".join([
        header,
        _le_bytes(lngs_q, "H"),
        _le_bytes(lats_q, "H"),
        _le_bytes(str_indices, "H"),
        names_bytes,
    ])

    compressed = gzip.compress(body, compresslevel=1)
    resp = Response(compressed, mimetype="application/octet-stream")
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Vary"] = "Accept-Encoding"
    g.meta["N"] = N
    g.meta["M"] = M
    return resp


@app.route("/routes")
def routes():
    try:
        parts = [float(x) for x in request.args.get("bbox", "").split(",")]
    except ValueError:
        abort(400)
    if len(parts) != 4:
        abort(400)
    w, s, e, n = parts
    features = []
    routes_map = {}
    stops_map = {}
    for path in _relevant_files("*.routes.sqlite",
                                 lambda p, *a: _rtree_overlaps(p, "route_rtree", *a),
                                 w, s, e, n):
        file_key = path.name[:-len(".routes.sqlite")]
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT d.coords, d.route_ids FROM route_data d "
                "JOIN route_rtree r ON d.id = r.id "
                "WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?",
                (e, w, n, s),
            ).fetchall()
            needed_rids = set()
            for coords_json, rids_json in rows:
                try:
                    coords = json.loads(coords_json)
                    local_rids = json.loads(rids_json) if rids_json else []
                except json.JSONDecodeError:
                    continue
                compound_rids = [f"{file_key}:{r}" for r in local_rids]
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {"route_ids": compound_rids},
                })
                for r in local_rids:
                    needed_rids.add(r)
            if needed_rids:
                placeholder = ",".join("?" * len(needed_rids))
                meta_rows = conn.execute(
                    f"SELECT id, ref, name, network, operator, colour, mode "
                    f"FROM route_meta WHERE id IN ({placeholder})",
                    tuple(needed_rids),
                ).fetchall()
                for row in meta_rows:
                    rid, ref, name, network, operator, colour, mode = row
                    routes_map[f"{file_key}:{rid}"] = {
                        "ref": ref, "name": name, "network": network,
                        "operator": operator, "colour": colour, "mode": mode,
                    }
                stop_rows = conn.execute(
                    f"SELECT route_id, node_id, ord, lng, lat, name, role "
                    f"FROM route_stop WHERE route_id IN ({placeholder}) "
                    f"ORDER BY route_id, ord",
                    tuple(needed_rids),
                ).fetchall()
                for rid, node_id, ord_, lng, lat, name, role in stop_rows:
                    stops_map.setdefault(f"{file_key}:{rid}", []).append({
                        "node_id": node_id,
                        "ord": ord_,
                        "lng": lng, "lat": lat,
                        "name": name, "role": role,
                    })
    return gzip_json({
        "type": "FeatureCollection",
        "features": features,
        "routes": routes_map,
        "stops": stops_map,
    })


@app.route("/walk-graph")
def walk_graph():
    """Binary walk-graph bundle.
    Layout (little-endian, 4-byte aligned throughout):
        Header (32 bytes):
            0:  'WALK' magic
            4:  u32 version (=1)
            8:  u32 N (node count)
            12: u32 E (edge count)
            16: u32 M (name count)
            20: u32 namesByteLen
            24: u32 shapesByteLen
            28: u32 reserved (0)
        Nodes (16N): N × f64 osm_id, N × f32 lng, N × f32 lat
        Edges (24E): E × u32 from_local, u32 to_local, f32 weight_m,
                     i32 name_idx (-1 none), u32 shape_off, u32 shape_len
        Names (namesByteLen): M × (u16 utf8_len + utf8 bytes)
        Shapes (shapesByteLen): concatenated shape bytes
    Nodes are keyed by OSM id (globally unique) for cross-response dedup.
    """
    try:
        parts = [float(x) for x in request.args.get("bbox", "").split(",")]
    except ValueError:
        abort(400)
    if len(parts) != 4:
        abort(400)
    w, s, e, n = parts

    osm_to_local = {}
    nodes_osm = []
    nodes_lng = []
    nodes_lat = []
    names_list = []
    name_to_idx = {}
    edges_from = []
    edges_to = []
    edges_weight = []
    edges_name_idx = []
    edges_shape_off = []
    edges_shape_len = []
    shape_chunks = []
    shapes_total = 0

    sql_bbox_ms = 0.0
    process_ms = 0.0
    total_edges_fetched = 0

    # Pre-bind hot dict/list methods so the per-row loop avoids attribute
    # lookups on globals.
    osm_get = osm_to_local.get
    nodes_osm_append = nodes_osm.append
    nodes_lng_append = nodes_lng.append
    nodes_lat_append = nodes_lat.append
    name_get = name_to_idx.get
    names_list_append = names_list.append
    edges_from_append = edges_from.append
    edges_to_append = edges_to.append
    edges_weight_append = edges_weight.append
    edges_name_idx_append = edges_name_idx.append
    edges_shape_off_append = edges_shape_off.append
    edges_shape_len_append = edges_shape_len.append
    shape_chunks_append = shape_chunks.append

    t_setup = time.perf_counter()
    walk_files = _relevant_files("*.walk.sqlite",
                                  lambda p, *a: _rtree_overlaps(p, "walk_node_rtree", *a),
                                  w, s, e, n)
    g.phases["setup"] = (time.perf_counter() - t_setup) * 1000.0
    g.meta["files"] = len(walk_files)

    # A single query fetches edges + both endpoint nodes + name text, joined
    # in sqlite. Split into two halves (from_id IN bbox / to_id IN bbox) so
    # each half uses its own endpoint index; the second half filters out
    # from_id-matches to keep them disjoint. UNION ALL is cheaper than UNION.
    edge_sql = (
        "SELECT e.weight_m, COALESCE(nm.text, ''), e.shape_blob, "
        "       nf.osm_id, nf.lng_u, nf.lat_u, "
        "       nt.osm_id, nt.lng_u, nt.lat_u "
        "FROM walk_edge e "
        "JOIN walk_node nf ON nf.id = e.from_id "
        "JOIN walk_node nt ON nt.id = e.to_id "
        "LEFT JOIN walk_name nm ON nm.id = e.name_id "
        "WHERE e.from_id IN (SELECT id FROM bbox_ids) "
        "UNION ALL "
        "SELECT e.weight_m, COALESCE(nm.text, ''), e.shape_blob, "
        "       nf.osm_id, nf.lng_u, nf.lat_u, "
        "       nt.osm_id, nt.lng_u, nt.lat_u "
        "FROM walk_edge e "
        "JOIN walk_node nf ON nf.id = e.from_id "
        "JOIN walk_node nt ON nt.id = e.to_id "
        "LEFT JOIN walk_name nm ON nm.id = e.name_id "
        "WHERE e.to_id IN (SELECT id FROM bbox_ids) "
        "  AND e.from_id NOT IN (SELECT id FROM bbox_ids)"
    )

    for path in walk_files:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(walk_node)")}
            if "osm_id" not in cols or "lng_u" not in cols:
                continue

            t_bbox = time.perf_counter()
            conn.execute("CREATE TEMP TABLE bbox_ids (id INTEGER PRIMARY KEY)")
            conn.execute(
                "INSERT INTO bbox_ids(id) "
                "SELECT id FROM walk_node_rtree "
                "WHERE minX <= ? AND maxX >= ? AND minY <= ? AND maxY >= ?",
                (e, w, n, s),
            )
            sql_bbox_ms += (time.perf_counter() - t_bbox) * 1000.0

            # Stream-iterate the cursor: rows are consumed as sqlite emits
            # them, without materialising the full 800k-row tuple list first.
            # Row tuple: (weight_m, name_text, shape_blob,
            #             from_osm, from_lng_u, from_lat_u,
            #             to_osm,   to_lng_u,   to_lat_u)
            t_process = time.perf_counter()
            row_count = 0
            for row in conn.execute(edge_sql):
                row_count += 1
                from_osm = row[3]
                to_osm = row[6]
                if from_osm is None or to_osm is None:
                    continue
                f_loc = osm_get(from_osm)
                if f_loc is None:
                    f_loc = len(nodes_osm)
                    osm_to_local[from_osm] = f_loc
                    nodes_osm_append(from_osm)
                    nodes_lng_append(row[4] / 1e6)
                    nodes_lat_append(row[5] / 1e6)
                t_loc = osm_get(to_osm)
                if t_loc is None:
                    t_loc = len(nodes_osm)
                    osm_to_local[to_osm] = t_loc
                    nodes_osm_append(to_osm)
                    nodes_lng_append(row[7] / 1e6)
                    nodes_lat_append(row[8] / 1e6)
                name_text = row[1]
                if name_text:
                    ni = name_get(name_text)
                    if ni is None:
                        ni = len(names_list)
                        names_list_append(name_text)
                        name_to_idx[name_text] = ni
                else:
                    ni = -1
                edges_from_append(f_loc)
                edges_to_append(t_loc)
                edges_weight_append(row[0])
                edges_name_idx_append(ni)
                shape_blob = row[2]
                if shape_blob:
                    edges_shape_off_append(shapes_total)
                    edges_shape_len_append(len(shape_blob))
                    shape_chunks_append(shape_blob)
                    shapes_total += len(shape_blob)
                else:
                    edges_shape_off_append(0)
                    edges_shape_len_append(0)
            process_ms += (time.perf_counter() - t_process) * 1000.0
            total_edges_fetched += row_count

            conn.execute("DROP TABLE bbox_ids")

    g.phases["sql_bbox"] = sql_bbox_ms
    g.phases["process"] = process_ms
    g.meta["N"] = len(nodes_osm)
    g.meta["E"] = len(edges_from)
    g.meta["E_fetched"] = total_edges_fetched

    N = len(nodes_osm)
    E = len(edges_from)
    M = len(names_list)

    with _phase("pack"):
        # Sort edges by weight ascending so we can encode the first chunk as
        # u8 (0..255 m), the next as u16 (256..65535 m), and the tail (very
        # rare) as f32. Two split indices in the header describe the ranges.
        # All edge-parallel arrays must be reordered together.
        import bisect
        order = sorted(range(E), key=lambda i: edges_weight[i])
        edges_weight    = [edges_weight[i]    for i in order]
        edges_from      = [edges_from[i]      for i in order]
        edges_to        = [edges_to[i]        for i in order]
        edges_name_idx  = [edges_name_idx[i]  for i in order]
        edges_shape_off = [edges_shape_off[i] for i in order]
        edges_shape_len = [edges_shape_len[i] for i in order]

        u8_end  = bisect.bisect_right(edges_weight, 255)
        u16_end = bisect.bisect_right(edges_weight, 65535)

        # name_idx width. -1 ("no name") encodes as sentinel (max value of the
        # chosen width) so we can still use unsigned arrays client-side.
        if M < 0xFF:
            name_idx_width = 1
            name_sentinel = 0xFF
            name_typecode = "B"
        elif M < 0xFFFF:
            name_idx_width = 2
            name_sentinel = 0xFFFF
            name_typecode = "H"
        else:
            name_idx_width = 4
            name_sentinel = 0xFFFFFFFF
            name_typecode = "I"
        edges_name_idx_enc = [
            (ni if ni >= 0 else name_sentinel) for ni in edges_name_idx
        ]

        def _align4(b):
            pad = (-len(b)) & 3
            return b + (b"\x00" * pad) if pad else b

        weight_u8_bytes  = _align4(_le_bytes(edges_weight[:u8_end], "B"))
        weight_u16_bytes = _align4(_le_bytes(edges_weight[u8_end:u16_end], "H"))
        weight_f32_bytes = _le_bytes([float(v) for v in edges_weight[u16_end:]], "f")
        name_idx_bytes   = _align4(_le_bytes(edges_name_idx_enc, name_typecode))

        # Shape columns: replace the per-edge (u32 off, u32 len) pair (which
        # spends 8 B/edge × E regardless of whether the edge has a shape —
        # and on a typical city ~90% of edges have no shape) with:
        #   - a 1-bit-per-edge "has shape" bitmap
        #   - sparse (u32 off, u16 len) pairs, one per has-shape edge only
        # For 850k edges × 10% has-shape, this drops from ~6.5 MB to ~600 KB.
        bitmap_raw = bytearray((E + 7) // 8)
        sparse_shape_off = []
        sparse_shape_len = []
        for i, slen in enumerate(edges_shape_len):
            if slen > 0:
                bitmap_raw[i >> 3] |= 1 << (i & 7)
                sparse_shape_off.append(edges_shape_off[i])
                # shape_len fits in u16: max single-edge shape blob is a
                # short varint-encoded polyline, well under 64 KB.
                sparse_shape_len.append(min(slen, 0xFFFF))
        shape_edge_count = len(sparse_shape_off)

        bitmap_bytes     = _align4(bytes(bitmap_raw))
        sparse_off_bytes = _le_bytes(sparse_shape_off, "I")
        sparse_len_bytes = _align4(_le_bytes(sparse_shape_len, "H"))

        names_buf = BytesIO()
        for name in names_list:
            b = name.encode("utf-8")
            if len(b) > 65535:
                b = b[:65535]
            names_buf.write(struct.pack("<H", len(b)))
            names_buf.write(b)
        names_bytes = names_buf.getvalue()
        shapes_bytes = b"".join(shape_chunks)

        # v3 header (48 bytes). Nodes stay 8-aligned for f64 osm_id view.
        header = struct.pack(
            "<4sIIIIIIIIIII",
            b"WALK", 3, N, E, M,
            len(names_bytes), len(shapes_bytes),
            u8_end, u16_end, name_idx_width,
            shape_edge_count, 0,
        )

        body = b"".join([
            header,
            _le_bytes(nodes_osm, "d"),
            _le_bytes(nodes_lng, "f"),
            _le_bytes(nodes_lat, "f"),
            _le_bytes(edges_from, "I"),
            _le_bytes(edges_to, "I"),
            weight_u8_bytes,
            weight_u16_bytes,
            weight_f32_bytes,
            name_idx_bytes,
            bitmap_bytes,
            sparse_off_bytes,
            sparse_len_bytes,
            names_bytes,
            shapes_bytes,
        ])
        g.meta["u8end"] = u8_end
        g.meta["u16end"] = u16_end
        g.meta["niw"] = name_idx_width
        g.meta["sec"] = shape_edge_count

    with _phase("gzip"):
        compressed = gzip.compress(body, compresslevel=1)

    resp = Response(compressed, mimetype="application/octet-stream")
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Vary"] = "Accept-Encoding"
    return resp


@app.route("/tiles/<int:z>/<int:x>/<int:y>.pbf")
def tile(z, x, y):
    paths = sorted(DATA_DIR.glob("*.mbtiles")) if DATA_DIR.exists() else []
    if not paths:
        abort(404)
    y_tms = (1 << z) - 1 - y
    best = None
    for path in paths:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT tile_data FROM tiles "
                "WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, y_tms),
            ).fetchone()
        if row is not None and (best is None or len(row[0]) > len(best)):
            best = row[0]
    if best is None:
        return Response(status=204)
    resp = Response(best, mimetype="application/x-protobuf")
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


def _in_chunks(iterable, size=500):
    buf = list(iterable)
    for start in range(0, len(buf), size):
        yield buf[start:start + size]


@app.route("/schedule")
def schedule():
    try:
        parts = [float(x) for x in request.args.get("bbox", "").split(",")]
    except ValueError:
        abort(400)
    if len(parts) != 4:
        abort(400)
    w, s, e, n = parts
    empty = {"stops": {}, "patterns": {}, "stop_patterns": {},
             "timings": [], "headsigns": [], "services": [],
             "trips": [], "routes": {}, "service_exceptions": []}
    if not SCHEDULE_PATH.exists():
        return gzip_json(empty)

    with sqlite3.connect(f"file:{SCHEDULE_PATH}?mode=ro", uri=True) as conn:
        stop_rows = conn.execute(
            "SELECT s.id, s.id_num, s.name, s.code, s.lng, s.lat "
            "FROM stop s JOIN stop_rtree r ON r.id_num = s.id_num "
            "WHERE r.minX <= ? AND r.maxX >= ? AND r.minY <= ? AND r.maxY >= ?",
            (e, w, n, s),
        ).fetchall()
        if not stop_rows:
            return gzip_json(empty)

        stop_nums = [row[1] for row in stop_rows]
        num_to_id = {row[1]: row[0] for row in stop_rows}
        stops = {}
        for sid, _num, nm, code, lng, lat in stop_rows:
            stops[sid] = {"name": nm, "code": code, "lng": lng, "lat": lat,
                          "osm_nodes": []}

        stop_ids = list(stops.keys())
        stop_id_ph = ",".join("?" * len(stop_ids))
        for gid, osm_nid in conn.execute(
            f"SELECT gtfs_stop_id, osm_node_id FROM gtfs_osm_link "
            f"WHERE gtfs_stop_id IN ({stop_id_ph})",
            tuple(stop_ids),
        ):
            if gid in stops:
                stops[gid]["osm_nodes"].append(osm_nid)

        # Reverse index: stop -> [[pattern, seq], ...]
        stop_patterns = {}
        needed_pattern_ids = set()
        for chunk in _in_chunks(stop_nums):
            ph = ",".join("?" * len(chunk))
            for stop_num, pattern_id, seq in conn.execute(
                f"SELECT stop_num, pattern_id, stop_seq "
                f"FROM pattern_stop WHERE stop_num IN ({ph})",
                tuple(chunk),
            ):
                sid_text = num_to_id.get(stop_num)
                if not sid_text: continue
                stop_patterns.setdefault(sid_text, []).append([pattern_id, seq])
                needed_pattern_ids.add(pattern_id)

        patterns = {}
        needed_route_ids = set()
        for chunk in _in_chunks(needed_pattern_ids):
            ph = ",".join("?" * len(chunk))
            for pid, route_id, stop_count, stops_blob in conn.execute(
                f"SELECT id, route_id, stop_count, stops_blob "
                f"FROM pattern WHERE id IN ({ph})",
                tuple(chunk),
            ):
                patterns[str(pid)] = {
                    "route_id": route_id,
                    "stop_count": stop_count,
                    "stops_b64": base64.b64encode(stops_blob).decode("ascii"),
                }
                if route_id: needed_route_ids.add(route_id)

        # Trips on those patterns — all INT refs + first_departure_sec
        trip_rows = []
        needed_timing_ids = set()
        needed_service_nums = set()
        needed_headsign_ids = set()
        needed_shape_nums = set()
        # shape_num may be absent on older DBs; query it via COALESCE / feature-detect.
        has_shape_col = any(
            r[1] == "shape_num"
            for r in conn.execute("PRAGMA table_info(trip)")
        )
        trip_sel = (
            "pattern_id, timing_id, service_num, headsign_id, "
            "direction, first_departure_sec"
            + (", shape_num" if has_shape_col else ", NULL")
        )
        for chunk in _in_chunks(needed_pattern_ids):
            ph = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT {trip_sel} FROM trip WHERE pattern_id IN ({ph})",
                tuple(chunk),
            ):
                trip_rows.append(list(row))
                needed_timing_ids.add(row[1])
                needed_service_nums.add(row[2])
                needed_headsign_ids.add(row[3])
                if row[6] is not None:
                    needed_shape_nums.add(row[6])

        # Interned lookups (dense local remap so client can use flat arrays)
        timing_map = {}
        timings = []  # indexed by local_timing_id
        for chunk in _in_chunks(needed_timing_ids):
            ph = ",".join("?" * len(chunk))
            for tid, blob in conn.execute(
                f"SELECT id, times_blob FROM trip_time WHERE id IN ({ph})",
                tuple(chunk),
            ):
                timing_map[tid] = len(timings)
                timings.append(base64.b64encode(blob).decode("ascii"))

        headsign_map = {}
        headsigns = []
        for chunk in _in_chunks(needed_headsign_ids):
            ph = ",".join("?" * len(chunk))
            for hid, text in conn.execute(
                f"SELECT id, text FROM headsign WHERE id IN ({ph})",
                tuple(chunk),
            ):
                headsign_map[hid] = len(headsigns)
                headsigns.append(text or "")

        service_map = {}
        services = []
        for chunk in _in_chunks(needed_service_nums):
            ph = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT id, service_id, monday, tuesday, wednesday, thursday, "
                f"friday, saturday, sunday, start_date, end_date "
                f"FROM service WHERE id IN ({ph})",
                tuple(chunk),
            ):
                sid_num, sid, mo, tu, we, th, fr, sa, su, sd, ed = row
                service_map[sid_num] = len(services)
                services.append({
                    "id": sid,
                    "dow": [mo, tu, we, th, fr, sa, su],
                    "start": sd, "end": ed,
                })

        service_exceptions = []
        for chunk in _in_chunks(needed_service_nums):
            ph = ",".join("?" * len(chunk))
            for svc_num, date, et in conn.execute(
                f"SELECT service_num, date, exception_type "
                f"FROM service_exception WHERE service_num IN ({ph})",
                tuple(chunk),
            ):
                local = service_map.get(svc_num)
                if local is None: continue
                service_exceptions.append([local, date, et])

        shape_map = {}
        shapes = []
        if has_shape_col and needed_shape_nums:
            for chunk in _in_chunks(needed_shape_nums):
                ph = ",".join("?" * len(chunk))
                for sid_num, blob in conn.execute(
                    f"SELECT id, points_blob FROM shape WHERE id IN ({ph})",
                    tuple(chunk),
                ):
                    shape_map[sid_num] = len(shapes)
                    shapes.append(base64.b64encode(blob).decode("ascii"))

        # Remap trip_rows using local indices
        trips = []
        for row in trip_rows:
            pat_id, timing_id, svc_num, hs_id, direction, first_dep, shape_num = row
            local_shape = shape_map.get(shape_num) if shape_num is not None else None
            trips.append([
                pat_id,
                timing_map[timing_id],
                service_map[svc_num],
                headsign_map[hs_id],
                direction, first_dep,
                local_shape if local_shape is not None else -1,
            ])

        routes = {}
        for chunk in _in_chunks(needed_route_ids):
            ph = ",".join("?" * len(chunk))
            for rid, short, long_, mode, colour in conn.execute(
                f"SELECT id, short_name, long_name, mode, colour "
                f"FROM route WHERE id IN ({ph})",
                tuple(chunk),
            ):
                routes[rid] = {
                    "short": short or "", "long": long_ or "",
                    "mode": mode, "colour": colour or "",
                }

    return gzip_json({
        "stops": stops,
        "patterns": patterns,
        "stop_patterns": stop_patterns,
        "timings": timings,
        "headsigns": headsigns,
        "services": services,
        "trips": trips,
        "shapes": shapes,
        "routes": routes,
        "service_exceptions": service_exceptions,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8464, debug=True)
