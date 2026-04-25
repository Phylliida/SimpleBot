#!/usr/bin/env python3
"""Build a compact walk graph by collapsing degree-2 shape points into polylines.

Two-pass algorithm:
  Pass 1 — walk all walkable ways, determine which nodes are intersections
           (endpoint of some way, OR appear in ≥2 ways). ~85% of OSM nodes
           are pure shape points and get dropped from the routing graph.
  Pass 2 — walk ways again with full node locations. Between consecutive
           intersections in each way, emit one polyline edge whose weight
           is cumulative haversine distance, with intermediate shape points
           packed into a zigzag-varint delta blob for later rendering.

Compared to the naive per-node graph this produces ~6–10× smaller files
(by empirical measurement on Canada-scale extracts).
"""
import math
import sqlite3
import sys
import time
import osmium


WALKABLE_HIGHWAYS = {
    "footway", "path", "pedestrian", "residential", "service",
    "tertiary", "tertiary_link", "secondary", "secondary_link",
    "primary", "primary_link", "track", "steps", "cycleway",
    "living_street", "unclassified", "road", "corridor",
}

EXCLUDED_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "construction", "proposed", "abandoned", "raceway",
}


def is_walkable(tags):
    foot = tags.get("foot", "")
    if foot in ("no", "private"):
        return False
    if tags.get("access") in ("no", "private"):
        return foot in ("yes", "designated", "permissive")
    if foot in ("yes", "designated", "permissive"):
        return True
    hw = tags.get("highway", "")
    if not hw:
        return False
    if hw in EXCLUDED_HIGHWAYS:
        return False
    return hw in WALKABLE_HIGHWAYS


def haversine_m(lon1, lat1, lon2, lat2):
    R = 6371000.0
    rlat1 = math.radians(lat1); rlat2 = math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def zigzag_varint_pack(values):
    """Encode signed ints via zigzag + LEB128."""
    out = bytearray()
    for v in values:
        if v >= 0:
            z = v << 1
        else:
            z = ((-v) << 1) - 1
        while z >= 0x80:
            out.append((z & 0x7F) | 0x80)
            z >>= 7
        out.append(z & 0x7F)
    return bytes(out)


# One degree of latitude ≈ 111320 m (equirectangular, good to ~0.5% globally).
_M_PER_DEG_LAT = 111320.0


def dp_simplify(points, tol_m):
    """Douglas-Peucker simplification. Accepts list of (lng, lat) degree
    pairs, returns a subset with endpoints preserved. Tolerance is in meters,
    measured in a local equirectangular projection around the segment's
    average latitude (so lng/lat aspect ratio is correct for short spans).
    Iterative to avoid Python recursion limits on very long ways."""
    n = len(points)
    if n <= 2:
        return list(points)
    lat_ref = sum(p[1] for p in points) / n
    cos_lat = math.cos(math.radians(lat_ref))
    mx = _M_PER_DEG_LAT * cos_lat
    my = _M_PER_DEG_LAT
    pm = [(p[0] * mx, p[1] * my) for p in points]
    keep = bytearray(n)
    keep[0] = 1
    keep[-1] = 1
    tol2 = tol_m * tol_m
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = pm[a]
        bx, by = pm[b]
        dx, dy = bx - ax, by - ay
        seg_len2 = dx * dx + dy * dy
        max_d2 = 0.0
        max_i = -1
        for i in range(a + 1, b):
            px, py = pm[i]
            if seg_len2 > 0.0:
                t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
                if t < 0.0: t = 0.0
                elif t > 1.0: t = 1.0
                projx, projy = ax + t * dx, ay + t * dy
                d2 = (px - projx) ** 2 + (py - projy) ** 2
            else:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            if d2 > max_d2:
                max_d2 = d2
                max_i = i
        if max_d2 > tol2:
            keep[max_i] = 1
            stack.append((a, max_i))
            stack.append((max_i, b))
    return [points[i] for i in range(n) if keep[i]]


class DegreeCounter(osmium.SimpleHandler):
    """Pass 1: identify intersection nodes.

    A node is an intersection iff it's an endpoint of any way OR appears in
    ≥2 ways. Pure middle-of-single-way nodes are shape points.
    """
    def __init__(self):
        super().__init__()
        self.seen_once = set()
        self.intersections = set()
        self.way_count = 0
        self.last_tick = time.time()

    def way(self, w):
        tags = {t.k: t.v for t in w.tags}
        if not is_walkable(tags):
            return
        try:
            nodes = [n.ref for n in w.nodes]
        except Exception:
            return
        if len(nodes) < 2:
            return
        for nid in nodes:
            if nid in self.intersections:
                continue
            if nid in self.seen_once:
                self.intersections.add(nid)
                self.seen_once.discard(nid)
            else:
                self.seen_once.add(nid)
        # Way endpoints are always intersections (dead-ends or branches)
        for nid in (nodes[0], nodes[-1]):
            self.intersections.add(nid)
            self.seen_once.discard(nid)
        self.way_count += 1
        if time.time() - self.last_tick > 1.0:
            sys.stderr.write(
                f"\r\033[K  pass 1: ways {self.way_count:,}  "
                f"intersections {len(self.intersections):,}  "
                f"shape-points {len(self.seen_once):,}"
            )
            sys.stderr.flush()
            self.last_tick = time.time()


class PolylineEmitter(osmium.SimpleHandler):
    """Pass 2: for each walkable way, walk through its nodes and emit one
    polyline edge between each pair of consecutive intersections.

    All writes are batched and committed in a single transaction for speed.
    """
    NODE_BATCH_SIZE = 50000
    EDGE_BATCH_SIZE = 50000

    def __init__(self, db, intersections):
        super().__init__()
        self.db = db
        self.intersections = intersections
        self.osm_to_seq = {}
        self.name_cache = {}
        self.node_batch = []      # [(seq, osm_id, lng_u, lat_u), ...]
        self.edge_batch = []      # [(from, to, weight_m, name_id, shape_blob), ...]
        self.edge_count = 0
        self.shape_count = 0
        self.way_count = 0
        self.last_tick = time.time()

    def _ensure_node(self, osm_id, lng, lat):
        seq = self.osm_to_seq.get(osm_id)
        if seq is not None:
            return seq
        seq = len(self.osm_to_seq) + 1
        self.osm_to_seq[osm_id] = seq
        lng_u = round(lng * 1_000_000)
        lat_u = round(lat * 1_000_000)
        self.node_batch.append((seq, osm_id, lng_u, lat_u))
        if len(self.node_batch) >= self.NODE_BATCH_SIZE:
            self._flush_nodes()
        return seq

    def _flush_nodes(self):
        if self.node_batch:
            self.db.executemany(
                "INSERT INTO walk_node(id, osm_id, lng_u, lat_u) VALUES (?, ?, ?, ?)",
                self.node_batch,
            )
            self.node_batch.clear()

    def _flush_edges(self):
        if self.edge_batch:
            self.db.executemany(
                "INSERT INTO walk_edge(from_id, to_id, weight_m, name_id, shape_blob) "
                "VALUES (?, ?, ?, ?, ?)",
                self.edge_batch,
            )
            self.edge_batch.clear()

    def flush_all(self):
        self._flush_nodes()
        self._flush_edges()

    def _intern_name(self, text):
        if not text:
            return None
        nid = self.name_cache.get(text)
        if nid is not None:
            return nid
        cur = self.db.execute(
            "INSERT INTO walk_name(text) VALUES (?) "
            "ON CONFLICT(text) DO NOTHING RETURNING id", (text,)
        )
        row = cur.fetchone()
        if row is None:
            row = self.db.execute(
                "SELECT id FROM walk_name WHERE text=?", (text,)
            ).fetchone()
        nid = row[0]
        self.name_cache[text] = nid
        return nid

    def way(self, w):
        tags = {t.k: t.v for t in w.tags}
        if not is_walkable(tags):
            return
        try:
            raw = list(w.nodes)
        except Exception:
            return
        valid = [(n.ref, n.location.lon, n.location.lat)
                 for n in raw if n.location.valid()]
        if len(valid) < 2:
            return
        name_id = self._intern_name(tags.get("name", ""))
        self.way_count += 1

        seg_start = 0
        seg_start_seq = self._ensure_node(*valid[0])
        intersections = self.intersections
        last_idx = len(valid) - 1
        for i in range(1, len(valid)):
            osm_id_i, lng_i, lat_i = valid[i]
            if i != last_idx and osm_id_i not in intersections:
                continue
            end_seq = self._ensure_node(osm_id_i, lng_i, lat_i)

            # Collect every point in this segment (endpoints + any shape
            # points) and compute the honest walking distance over all of
            # them — routing accuracy should not change with shape compression.
            seg_points = [(valid[seg_start][1], valid[seg_start][2])]
            for j in range(seg_start + 1, i):
                seg_points.append((valid[j][1], valid[j][2]))
            seg_points.append((lng_i, lat_i))
            weight_m = 0.0
            for k in range(1, len(seg_points)):
                weight_m += haversine_m(
                    seg_points[k - 1][0], seg_points[k - 1][1],
                    seg_points[k][0],     seg_points[k][1],
                )
            weight_m_int = max(1, round(weight_m))

            # Shape: DP-simplify at 1m, drop entirely when only the endpoints
            # remain, otherwise emit intermediate points snapped to 1m (5-
            # decimal) precision. Encoding is still microdegree deltas from
            # the full-precision start endpoint, to keep the wire format
            # unchanged — quantized vertices just happen to fall on a 10-unit
            # grid in microdegrees.
            shape_blob = None
            if len(seg_points) > 2:
                # 3 m tolerance: imperceptible at pedestrian zoom, and most
                # straight OSM segments collapse to just endpoints (which then
                # hit the "no shape" fast path below).
                simplified = dp_simplify(seg_points, 3.0)
                if len(simplified) > 2:
                    shape_deltas = []
                    prev_lng_u = round(seg_points[0][0] * 1_000_000)
                    prev_lat_u = round(seg_points[0][1] * 1_000_000)
                    for p in simplified[1:-1]:
                        lng_u = round(p[0] * 100_000) * 10
                        lat_u = round(p[1] * 100_000) * 10
                        shape_deltas.append(lng_u - prev_lng_u)
                        shape_deltas.append(lat_u - prev_lat_u)
                        prev_lng_u, prev_lat_u = lng_u, lat_u
                    if shape_deltas:
                        shape_blob = zigzag_varint_pack(shape_deltas)

            if shape_blob is not None:
                self.shape_count += 1
            if seg_start_seq != end_seq or shape_blob is not None:
                self.edge_batch.append(
                    (seg_start_seq, end_seq, weight_m_int, name_id, shape_blob)
                )
                self.edge_count += 1
                if len(self.edge_batch) >= self.EDGE_BATCH_SIZE:
                    self._flush_edges()
            seg_start = i
            seg_start_seq = end_seq

        if time.time() - self.last_tick > 1.0:
            sys.stderr.write(
                f"\r\033[K  pass 2: ways {self.way_count:,}  "
                f"nodes {len(self.osm_to_seq):,}  edges {self.edge_count:,}  "
                f"with-shape {self.shape_count:,}  names {len(self.name_cache):,}"
            )
            sys.stderr.flush()
            self.last_tick = time.time()


def main(pbf, dst):
    db = sqlite3.connect(dst)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript("""
    DROP TABLE IF EXISTS walk_node;
    DROP TABLE IF EXISTS walk_edge;
    DROP TABLE IF EXISTS walk_node_rtree;
    DROP TABLE IF EXISTS walk_name;

    CREATE TABLE walk_node(
        id INTEGER PRIMARY KEY,
        osm_id INTEGER,
        lng_u INTEGER NOT NULL,
        lat_u INTEGER NOT NULL
    );
    CREATE VIRTUAL TABLE walk_node_rtree USING rtree(
        id, minX, maxX, minY, maxY
    );
    CREATE TABLE walk_name(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT UNIQUE NOT NULL
    );
    CREATE TABLE walk_edge(
        from_id INTEGER NOT NULL,
        to_id INTEGER NOT NULL,
        weight_m INTEGER NOT NULL,
        name_id INTEGER,
        shape_blob BLOB
    );
    """)
    db.commit()

    # Pass 1
    t = time.time()
    sys.stderr.write("pass 1: identifying intersections\n"); sys.stderr.flush()
    dc = DegreeCounter()
    dc.apply_file(pbf)
    sys.stderr.write(
        f"\r\033[K  pass 1 done: {dc.way_count:,} ways  "
        f"{len(dc.intersections):,} intersections  "
        f"{len(dc.seen_once):,} shape-points  "
        f"({time.time() - t:.1f}s)\n"
    )

    # Pass 2
    t = time.time()
    sys.stderr.write("pass 2: emitting polyline edges\n"); sys.stderr.flush()
    intersections = dc.intersections
    dc.seen_once.clear()
    dc = None
    emitter = PolylineEmitter(db, intersections)
    db.execute("BEGIN")
    emitter.apply_file(pbf, locations=True)
    emitter.flush_all()
    db.commit()
    sys.stderr.write(
        f"\r\033[K  pass 2 done: {emitter.way_count:,} ways  "
        f"{len(emitter.osm_to_seq):,} nodes  {emitter.edge_count:,} edges  "
        f"{emitter.shape_count:,} with shape  "
        f"{len(emitter.name_cache):,} names  "
        f"({time.time() - t:.1f}s)\n"
    )

    # Populate rtree in bulk from walk_node (much faster than per-row inserts).
    sys.stderr.write("populating rtree\n"); sys.stderr.flush()
    t = time.time()
    db.execute(
        "INSERT INTO walk_node_rtree(id, minX, maxX, minY, maxY) "
        "SELECT id, lng_u / 1000000.0, lng_u / 1000000.0, "
        "       lat_u / 1000000.0, lat_u / 1000000.0 FROM walk_node"
    )
    db.commit()
    sys.stderr.write(f"  rtree populated in {time.time() - t:.1f}s\n")

    sys.stderr.write("building edge indexes\n"); sys.stderr.flush()
    t = time.time()
    db.execute("CREATE INDEX idx_walk_edge_from ON walk_edge(from_id)")
    db.execute("CREATE INDEX idx_walk_edge_to ON walk_edge(to_id)")
    db.commit()
    sys.stderr.write(f"  indexes built in {time.time() - t:.1f}s\n")

    sys.stderr.write("VACUUM…\n"); sys.stderr.flush()
    db.execute("VACUUM")
    db.commit()
    db.close()
    sys.stderr.write(f"  → {dst}\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: build-walk-graph.py input.osm.pbf output.sqlite", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
