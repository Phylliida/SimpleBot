#!/usr/bin/env python3
"""Extract addr:housenumber features from an OSM .pbf into a sqlite db
consumed by the /housenumbers endpoint. Writes an rtree over the point
locations so bbox queries stay fast on city-sized regions.

Usage: python3 build-housenumbers.py input.osm.pbf output.sqlite
"""
import sqlite3
import sys

import osmium


class Handler(osmium.SimpleHandler):
    """Collect (lng, lat, text) rows for every node/way carrying an
    addr:housenumber tag. Ways are reduced to their centroid."""

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.batch = []
        self.count = 0

    def _flush(self):
        if self.batch:
            self.db.executemany(
                "INSERT INTO hn(lng_u, lat_u, text) VALUES (?, ?, ?)",
                self.batch,
            )
            self.count += len(self.batch)
            self.batch.clear()

    def _emit(self, lng, lat, text):
        self.batch.append((
            round(lng * 1_000_000),
            round(lat * 1_000_000),
            text,
        ))
        if len(self.batch) >= 20000:
            self._flush()

    def node(self, n):
        text = n.tags.get("addr:housenumber")
        if not text or not n.location.valid():
            return
        self._emit(n.location.lon, n.location.lat, text)

    def way(self, w):
        text = w.tags.get("addr:housenumber")
        if not text:
            return
        sx = sy = 0.0
        cnt = 0
        try:
            for nref in w.nodes:
                if nref.location.valid():
                    sx += nref.location.lon
                    sy += nref.location.lat
                    cnt += 1
        except Exception:
            return
        if cnt == 0:
            return
        self._emit(sx / cnt, sy / cnt, text)


def main():
    if len(sys.argv) != 3:
        print("usage: build-housenumbers.py input.osm.pbf output.sqlite", file=sys.stderr)
        sys.exit(1)
    src, dst = sys.argv[1], sys.argv[2]

    db = sqlite3.connect(dst)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.executescript("""
        DROP TABLE IF EXISTS hn;
        DROP TABLE IF EXISTS hn_rtree;
        CREATE TABLE hn (
            id INTEGER PRIMARY KEY,
            lng_u INTEGER NOT NULL,
            lat_u INTEGER NOT NULL,
            text TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE hn_rtree USING rtree(
            id, minX, maxX, minY, maxY
        );
    """)

    h = Handler(db)
    h.apply_file(src, locations=True)
    h._flush()

    db.execute(
        "INSERT INTO hn_rtree(id, minX, maxX, minY, maxY) "
        "SELECT id, lng_u/1000000.0, lng_u/1000000.0, "
        "       lat_u/1000000.0, lat_u/1000000.0 FROM hn"
    )
    db.commit()
    db.close()
    print(f"{h.count:,} housenumbers -> {dst}")


if __name__ == "__main__":
    main()
