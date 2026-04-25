#!/usr/bin/env python3
import json
import sqlite3
import sys
import time
import osmium

TRANSIT_MODES = {
    "bus", "trolleybus", "share_taxi",
    "subway", "tram", "light_rail",
    "train", "monorail",
}

STOP_ROLES = {
    "stop", "stop_entry_only", "stop_exit_only",
    "platform", "platform_entry_only", "platform_exit_only",
}


class RelationReader(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.routes = []             # [{ref, name, network, operator, colour, mode}, ...]
        self.way_to_routes = {}      # way_id -> [route_idx]
        self.stop_members = []       # [(rid, node_id, order, role)]

    def relation(self, r):
        tags = {t.k: t.v for t in r.tags}
        if tags.get("type") != "route":
            return
        mode = tags.get("route", "")
        if mode not in TRANSIT_MODES:
            return
        rid = len(self.routes)
        self.routes.append({
            "ref":      tags.get("ref", ""),
            "name":     tags.get("name", ""),
            "network":  tags.get("network", ""),
            "operator": tags.get("operator", ""),
            "colour":   tags.get("colour", "") or tags.get("color", ""),
            "mode":     mode,
        })
        stop_order = 0
        for m in r.members:
            if m.type == "w":
                self.way_to_routes.setdefault(m.ref, []).append(rid)
            elif m.type == "n":
                role = m.role or ""
                if role in STOP_ROLES:
                    self.stop_members.append((rid, m.ref, stop_order, role))
                    stop_order += 1


class WayNodeWriter(osmium.SimpleHandler):
    def __init__(self, way_to_routes, stop_members, db):
        super().__init__()
        self.way_to_routes = way_to_routes
        self.db = db
        self.counter = 0
        self.stops_written = 0
        self.last_tick = time.time()
        # group stop_members by node_id for fast node-time lookup
        self.node_to_stops = {}
        for rid, nid, order, role in stop_members:
            self.node_to_stops.setdefault(nid, []).append((rid, order, role))

    def way(self, w):
        rel_ids = self.way_to_routes.get(w.id)
        if not rel_ids:
            return
        try:
            coords = []
            for n in w.nodes:
                if n.location.valid():
                    coords.append([n.location.lon, n.location.lat])
        except Exception:
            return
        if len(coords) < 2:
            return
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        self.db.execute(
            "INSERT INTO route_rtree VALUES (?, ?, ?, ?, ?)",
            (self.counter, min(xs), max(xs), min(ys), max(ys)),
        )
        self.db.execute(
            "INSERT INTO route_data(id, coords, route_ids) VALUES (?, ?, ?)",
            (self.counter, json.dumps(coords), json.dumps(rel_ids)),
        )
        self.counter += 1
        if time.time() - self.last_tick > 1.0:
            sys.stderr.write(
                f"\r\033[K  segments: {self.counter:,}  stops: {self.stops_written:,}"
            )
            sys.stderr.flush()
            self.last_tick = time.time()

    def node(self, n):
        entries = self.node_to_stops.get(n.id)
        if not entries:
            return
        if not n.location.valid():
            return
        tags = {t.k: t.v for t in n.tags}
        name = tags.get("name", "")
        lng, lat = n.location.lon, n.location.lat
        for rid, order, role in entries:
            self.db.execute(
                "INSERT INTO route_stop(route_id, node_id, ord, lng, lat, name, role) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rid, n.id, order, lng, lat, name, role),
            )
            self.stops_written += 1


def main(pbf, dst):
    db = sqlite3.connect(dst)
    db.execute("DROP TABLE IF EXISTS route_rtree")
    db.execute("DROP TABLE IF EXISTS route_data")
    db.execute("DROP TABLE IF EXISTS route_meta")
    db.execute("DROP TABLE IF EXISTS route_stop")
    db.execute(
        "CREATE VIRTUAL TABLE route_rtree USING rtree(id, minX, maxX, minY, maxY)"
    )
    db.execute(
        "CREATE TABLE route_data(id INTEGER PRIMARY KEY, coords TEXT, route_ids TEXT)"
    )
    db.execute(
        "CREATE TABLE route_meta(id INTEGER PRIMARY KEY, ref TEXT, name TEXT, "
        "network TEXT, operator TEXT, colour TEXT, mode TEXT)"
    )
    db.execute(
        "CREATE TABLE route_stop(route_id INTEGER, node_id INTEGER, ord INTEGER, "
        "lng REAL, lat REAL, name TEXT, role TEXT)"
    )
    db.execute("CREATE INDEX idx_route_stop_rid ON route_stop(route_id)")

    t = time.time()
    sys.stderr.write("  pass 1: reading relations…\n"); sys.stderr.flush()
    rh = RelationReader()
    rh.apply_file(pbf)
    sys.stderr.write(
        f"    {len(rh.routes)} routes, "
        f"{len(rh.way_to_routes)} unique member ways, "
        f"{len(rh.stop_members)} stop memberships "
        f"({time.time() - t:.1f}s)\n"
    )

    for i, r in enumerate(rh.routes):
        db.execute(
            "INSERT INTO route_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
            (i, r["ref"], r["name"], r["network"], r["operator"], r["colour"], r["mode"]),
        )

    t = time.time()
    sys.stderr.write("  pass 2: reading ways + stop nodes…\n"); sys.stderr.flush()
    ww = WayNodeWriter(rh.way_to_routes, rh.stop_members, db)
    ww.apply_file(pbf, locations=True)
    sys.stderr.write(
        f"\r\033[K    {ww.counter} segments, {ww.stops_written} stops "
        f"({time.time() - t:.1f}s)\n"
    )
    sys.stderr.flush()

    db.commit()
    db.close()
    sys.stderr.write(f"  → {dst}\n"); sys.stderr.flush()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: build-routes-db.py input.osm.pbf output.sqlite", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
