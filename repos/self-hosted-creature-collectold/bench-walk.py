#!/usr/bin/env python3
"""Download /walk-graph for a given bbox and dump a byte-level breakdown of
the binary bundle. Useful for spotting which column is the next best target
for compression.

    python3 bench-walk.py                  # defaults: localhost, Montreal bbox
    python3 bench-walk.py --bbox W,S,E,N
    python3 bench-walk.py --url https://poke.phylliidaassets.org
"""
import argparse
import gzip
import struct
import sys
import time
import urllib.request
from collections import Counter


def fmt(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1048576:.2f} MB"


def align4(x):
    return (x + 3) & ~3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8465")
    ap.add_argument(
        "--bbox",
        default="-73.94217484984556,45.040935177281284,"
                "-73.31660638748862,45.93918093916304",
        help="comma-separated W,S,E,N",
    )
    args = ap.parse_args()

    full = f"{args.url}/walk-graph?bbox={args.bbox}"
    req = urllib.request.Request(
        full,
        headers={"X-Download": "1", "Accept-Encoding": "gzip"},
    )

    t0 = time.perf_counter()
    with urllib.request.urlopen(req) as r:
        ce = r.headers.get("Content-Encoding", "")
        wire = r.read()
    elapsed_s = time.perf_counter() - t0
    body = gzip.decompress(wire) if ce == "gzip" else wire

    print(f"url     : {full}")
    print(f"time    : {elapsed_s:.2f} s")
    print(f"wire    : {fmt(len(wire))}  (gzip)")
    print(f"raw body: {fmt(len(body))}")
    if len(body):
        print(f"gzip    : {len(wire) / len(body):.1%} of raw")
    print()

    if len(body) < 48:
        print("body smaller than header — nothing to parse")
        sys.exit(1)

    magic, version, N, E, M, names_len, shapes_len, u8_end, u16_end, niw, shape_ec, _r = (
        struct.unpack_from("<4sIIIIIIIIIII", body, 0)
    )
    if magic != b"WALK":
        print(f"bad magic {magic!r}")
        sys.exit(1)

    print(f"version : {version}")
    print(f"N nodes : {N:,}")
    print(f"E edges : {E:,}")
    print(f"M names : {M:,}   (name_idx width: {niw} byte{'s' if niw > 1 else ''})")
    print(f"shaped  : {shape_ec:,} / {E:,} "
          f"({(shape_ec / E * 100) if E else 0:.1f}% of edges have shapes)")
    if E:
        print(
            f"weight  : u8={u8_end:,} ({u8_end / E:.1%})  "
            f"u16={u16_end - u8_end:,} ({(u16_end - u8_end) / E:.1%})  "
            f"f32={E - u16_end:,} ({(E - u16_end) / E:.1%})"
        )
    print()

    # Byte offsets match the layout produced by run.py /walk-graph v3.
    h = 48
    off_edges4 = h + 16 * N                  # from, to (no shape cols)
    off_weights = off_edges4 + 8 * E
    off_name_idx = (
        off_weights
        + align4(u8_end)
        + align4(2 * (u16_end - u8_end))
        + 4 * (E - u16_end)
    )
    off_bitmap = off_name_idx + align4(niw * E)
    bitmap_bytes = align4((E + 7) // 8)
    off_sparse_off = off_bitmap + bitmap_bytes
    off_sparse_len = off_sparse_off + 4 * shape_ec
    sparse_len_bytes = align4(2 * shape_ec)
    off_names = off_sparse_len + sparse_len_bytes
    off_shapes = off_names + names_len
    end = off_shapes + shapes_len

    if end != len(body):
        print(f"WARN: computed layout end {end} ≠ body {len(body)}")

    rows = [
        ("header",                               48),
        ("node osm_ids (f64)",                   8 * N),
        ("node lng (f32)",                       4 * N),
        ("node lat (f32)",                       4 * N),
        ("edge from (u32)",                      4 * E),
        ("edge to (u32)",                        4 * E),
        ("edge weight u8",                       align4(u8_end)),
        ("edge weight u16",                      align4(2 * (u16_end - u8_end))),
        ("edge weight f32",                      4 * (E - u16_end)),
        (f"edge name_idx (u{niw * 8})",          align4(niw * E)),
        ("has-shape bitmap",                     bitmap_bytes),
        ("sparse shape_off (u32)",               4 * shape_ec),
        ("sparse shape_len (u16)",               sparse_len_bytes),
        ("names pool",                           names_len),
        ("shape blob",                           shapes_len),
    ]

    print(f"{'column':35s} {'bytes':>12s} {'%':>6s}")
    print("-" * 55)
    total = 0
    for name, b in rows:
        total += b
        pct = b / len(body) * 100 if body else 0
        print(f"{name:35s} {fmt(b):>12s} {pct:>5.1f}%")
    print("-" * 55)
    print(f"{'sum':35s} {fmt(total):>12s}")
    print()

    # Shape length distribution (from the sparse u16 len column).
    if shape_ec:
        lens = struct.unpack_from(f"<{shape_ec}H", body, off_sparse_len)
        total_shape = sum(lens)
        longest_10pct = sum(sorted(lens, reverse=True)[: max(1, shape_ec // 10)])
        print(f"mean len on shaped edges: {total_shape / shape_ec:.1f} B")
        if total_shape:
            print(
                f"top 10% of shaped edges carry "
                f"{longest_10pct / total_shape:.0%} of shape bytes"
            )
    print()

    # Weight distribution (first 16 buckets of u8 range + three bucket summary)
    if E:
        wt_off = off_weights
        u8_weights = list(body[wt_off: wt_off + u8_end])
        if u8_weights:
            c = Counter()
            for w in u8_weights:
                c[w // 16] += 1
            print("u8 weight histogram (meters, 16-m buckets):")
            for bucket in sorted(c):
                lo, hi = bucket * 16, bucket * 16 + 15
                bar = "#" * int(c[bucket] / max(c.values()) * 40)
                print(f"  {lo:3d}-{hi:3d}m: {c[bucket]:7,d}  {bar}")
    print()

    # Quick back-of-envelope for the next potential wins.
    from_to_u24 = 6 * E            # pack u32 from/to into u24 if N < 16M
    node_coord_u24 = 6 * N         # pack f32 lng + f32 lat into u24 in bbox-relative units
    print("hypothetical further IDB wins:")
    print(f"  from/to → u24: {fmt(8 * E)} → {fmt(from_to_u24)} "
          f"(saves {fmt(8 * E - from_to_u24)})")
    print(f"  lng+lat → u24 bbox-relative: {fmt(8 * N)} → {fmt(node_coord_u24)} "
          f"(saves {fmt(8 * N - node_coord_u24)})")


if __name__ == "__main__":
    main()
