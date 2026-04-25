#!/usr/bin/env python3
"""Add an rtree spatial index to an existing .pois.sqlite file.

The POI DB was originally built with flat idx_lat + idx_lng indexes. SQLite
can only use one of those per query, so viewport bbox lookups scan a whole
latitude band (~100-500 ms per query). An rtree drops that to <5 ms.

This is idempotent: drops any existing poi_rtree before rebuilding.

Usage: add-poi-rtree.py data/north-america-latest.pois.sqlite
"""
import os
import sqlite3
import sys
import time


BATCH = 200_000


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    dst = sys.argv[1]
    if not os.path.exists(dst):
        print(f"no such file: {dst}")
        sys.exit(1)

    t_start = time.time()
    db = sqlite3.connect(dst)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.execute("PRAGMA cache_size = -200000")  # 200 MB page cache

    db.executescript("""
        DROP TABLE IF EXISTS poi_rtree;
        CREATE VIRTUAL TABLE poi_rtree USING rtree(
            id, minX, maxX, minY, maxY
        );
    """)

    total = db.execute("SELECT COUNT(*) FROM poi").fetchone()[0]
    print(f"populating rtree for {total:,} POIs…")

    inserted = 0
    t0 = time.time()
    read = db.cursor()
    # iterate by rowid — guarantees stable ordering and avoids loading the
    # whole table into memory at once.
    read.execute("SELECT rowid, lng, lat FROM poi ORDER BY rowid")

    batch = []
    while True:
        row = read.fetchone()
        if row is None:
            if batch:
                db.executemany(
                    "INSERT INTO poi_rtree(id, minX, maxX, minY, maxY) "
                    "VALUES (?, ?, ?, ?, ?)",
                    batch,
                )
                inserted += len(batch)
                batch.clear()
            break
        rid, lng, lat = row
        batch.append((rid, lng, lng, lat, lat))
        if len(batch) >= BATCH:
            db.executemany(
                "INSERT INTO poi_rtree(id, minX, maxX, minY, maxY) "
                "VALUES (?, ?, ?, ?, ?)",
                batch,
            )
            inserted += len(batch)
            batch.clear()
            elapsed = time.time() - t0
            rate = inserted / elapsed if elapsed else 0
            eta = (total - inserted) / rate if rate else 0
            pct = 100 * inserted / total
            print(
                f"  {inserted:>10,} / {total:,}  ({pct:5.1f}%)  "
                f"{rate/1000:>5.0f}k rows/s  eta {eta:>4.0f}s",
                flush=True,
            )

    db.commit()
    t_insert = time.time() - t0
    print(f"rtree populated in {t_insert:.1f}s")

    # Checkpoint WAL so the final file size is accurate before reporting.
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()

    size_gb = os.path.getsize(dst) / 1e9
    wall = time.time() - t_start
    print(f"{inserted:,} rows indexed → {dst}")
    print(f"final size: {size_gb:.2f} GB  (wall time {wall:.1f}s)")


if __name__ == "__main__":
    main()
