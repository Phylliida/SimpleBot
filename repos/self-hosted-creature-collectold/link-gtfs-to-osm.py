#!/usr/bin/env python3
"""Join GTFS stops to OSM route_stop.node_id by proximity + fuzzy name match.

Usage: link-gtfs-to-osm.py <schedule.sqlite> <routes-osm1.sqlite> [<routes-osm2.sqlite> ...]

Writes a `gtfs_osm_link(osm_node_id, gtfs_stop_id, distance_m, name_score)` table
into the schedule DB so the server can answer "for this OSM node, which GTFS
stops serve it?" at bbox-download time.
"""
import math
import sqlite3
import sys
import time
import unicodedata
from difflib import SequenceMatcher


GRID_DEG = 1000  # 1/1000 deg buckets ~ 100m
MAX_DISTANCE_M = 30.0
NAME_SCORE_THRESHOLD = 0.6
STRICT_DISTANCE_M = 15.0  # below this, accept even poor name matches


STOP_WORDS = {
    "station", "gare", "terminus", "arret", "arrêt",
    "bus", "stop", "quai", "plateforme", "platform",
    "métro", "metro", "de", "du", "des", "la", "le", "les",
    "l", "d", "und", "the", "a",
    "accès", "acces", "entrée", "entree", "entrance",
    "est", "ouest", "nord", "sud", "east", "west", "north", "south",
}


def normalize(s):
    if not s:
        return ""
    s = s.lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    out = []
    current = []
    for c in s:
        if c.isalnum():
            current.append(c)
        else:
            if current:
                out.append("".join(current))
                current = []
    if current:
        out.append("".join(current))
    tokens = [w for w in out if w and w not in STOP_WORDS]
    return " ".join(tokens)


def name_score(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def haversine_m(lng1, lat1, lng2, lat2):
    R = 6371000
    to_rad = math.radians
    dlat = to_rad(lat2 - lat1)
    dlng = to_rad(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(to_rad(lat1)) * math.cos(to_rad(lat2)) * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def grid_key(lng, lat):
    return (round(lng * GRID_DEG), round(lat * GRID_DEG))


def build_gtfs_grid(conn):
    grid = {}
    for stop_id, name, lng, lat in conn.execute(
        "SELECT id, name, lng, lat FROM stop"
    ):
        grid.setdefault(grid_key(lng, lat), []).append((stop_id, name, lng, lat))
    return grid


def load_osm_stops(paths):
    """Deduplicate OSM route_stop rows by node_id, return list of (node_id, name, lng, lat)."""
    seen = {}
    for p in paths:
        with sqlite3.connect(f"file:{p}?mode=ro", uri=True) as c:
            for node_id, name, lng, lat in c.execute(
                "SELECT node_id, name, lng, lat FROM route_stop"
            ):
                # Prefer the row with a non-empty name if we see the node twice
                prev = seen.get(node_id)
                if prev and prev[0]:
                    continue
                seen[node_id] = (name, lng, lat)
    return [(nid, n, lng, lat) for nid, (n, lng, lat) in seen.items()]


def main(args):
    if len(args) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    schedule_path = args[0]
    osm_paths = args[1:]

    sdb = sqlite3.connect(schedule_path)
    sdb.execute("DROP TABLE IF EXISTS gtfs_osm_link")
    sdb.execute("""
        CREATE TABLE gtfs_osm_link(
            osm_node_id INTEGER,
            gtfs_stop_id TEXT,
            distance_m REAL,
            name_score REAL
        )
    """)
    sdb.execute("CREATE INDEX idx_link_osm ON gtfs_osm_link(osm_node_id)")
    sdb.execute("CREATE INDEX idx_link_gtfs ON gtfs_osm_link(gtfs_stop_id)")

    sys.stderr.write("  building GTFS grid…\n"); sys.stderr.flush()
    grid = build_gtfs_grid(sdb)
    sys.stderr.write(f"    {sum(len(v) for v in grid.values()):,} gtfs stops in {len(grid):,} cells\n")

    sys.stderr.write("  loading OSM stops…\n"); sys.stderr.flush()
    osm_stops = load_osm_stops(osm_paths)
    sys.stderr.write(f"    {len(osm_stops):,} unique OSM nodes\n")

    sys.stderr.write("  matching…\n"); sys.stderr.flush()
    t0 = time.time()
    last_tick = t0
    total = 0
    unmatched = 0
    batch = []
    for i, (nid, oname, olng, olat) in enumerate(osm_stops):
        cx, cy = grid_key(olng, olat)
        candidates = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cell = grid.get((cx + dx, cy + dy))
                if not cell:
                    continue
                for gid, gname, glng, glat in cell:
                    d = haversine_m(olng, olat, glng, glat)
                    if d > MAX_DISTANCE_M:
                        continue
                    s = name_score(oname or "", gname or "")
                    if s >= NAME_SCORE_THRESHOLD or d <= STRICT_DISTANCE_M:
                        candidates.append((gid, d, s))
        if not candidates:
            unmatched += 1
        for gid, d, s in candidates:
            batch.append((nid, gid, d, s))
            total += 1
        if len(batch) >= 5000:
            sdb.executemany("INSERT INTO gtfs_osm_link VALUES (?, ?, ?, ?)", batch)
            batch = []
        if time.time() - last_tick > 1.0:
            sys.stderr.write(f"\r\033[K    processed {i + 1:,}/{len(osm_stops):,}  links {total:,}")
            sys.stderr.flush()
            last_tick = time.time()
    if batch:
        sdb.executemany("INSERT INTO gtfs_osm_link VALUES (?, ?, ?, ?)", batch)

    sys.stderr.write(
        f"\r\033[K    {total:,} links written, "
        f"{len(osm_stops) - unmatched:,}/{len(osm_stops):,} OSM nodes matched "
        f"({time.time() - t0:.1f}s)\n"
    )
    sdb.commit()
    sdb.close()


if __name__ == "__main__":
    main(sys.argv[1:])
