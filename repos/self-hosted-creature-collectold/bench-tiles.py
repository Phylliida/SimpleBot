#!/usr/bin/env python3
"""Scan data/*.mbtiles inside a bbox and print per-layer / per-zoom byte
stats for the vector tiles. Reads mbtiles directly (no server needed) and
parses the minimum amount of MVT protobuf required to list layers.

    python3 bench-tiles.py                          # defaults: Montreal bbox, z0..14
    python3 bench-tiles.py --bbox W,S,E,N --maxzoom 13
    python3 bench-tiles.py --topk 5                 # extra: show top-5 heaviest tiles
"""
import argparse
import gzip
import math
import pathlib
import sqlite3
import struct
from collections import defaultdict


def fmt(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1048576:.2f} MB"


def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    lat_rad = math.radians(lat)
    y = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


# --- minimal MVT protobuf parser ------------------------------------------
# Tile = repeated Layer @ field 3 (wire 2)
# Layer fields we care about:
#   1 (wire 2) name
#   2 (wire 2) Feature  (count by iterating)
#   3 (wire 2) keys     (string)
#   4 (wire 2) Value    (submessage)
#   5 (wire 0) extent
# Each Feature (wire 2 submessage):
#   1 (wire 0) id
#   2 (wire 2) tags (packed varints)
#   3 (wire 0) type
#   4 (wire 2) geometry (packed varints)


def _varint(buf, off):
    r, shift = 0, 0
    while True:
        b = buf[off]
        off += 1
        r |= (b & 0x7F) << shift
        if not (b & 0x80):
            return r, off
        shift += 7


def _skip(buf, off, wire):
    if wire == 0:
        _, off = _varint(buf, off)
    elif wire == 1:
        off += 8
    elif wire == 2:
        ln, off = _varint(buf, off)
        off += ln
    elif wire == 5:
        off += 4
    else:
        raise ValueError(f"unknown wire type {wire}")
    return off


def iter_layer_slices(tile_buf):
    """Yield (name, layer_bytes) for each Layer in a decoded tile.pbf."""
    off = 0
    while off < len(tile_buf):
        tag, off = _varint(tile_buf, off)
        field, wire = tag >> 3, tag & 7
        if field == 3 and wire == 2:
            ln, off = _varint(tile_buf, off)
            layer = tile_buf[off:off + ln]
            off += ln
            # Peek at the name field (Layer.name = 1, wire 2).
            name = "?"
            lo = 0
            while lo < len(layer):
                lt, lo = _varint(layer, lo)
                lf, lw = lt >> 3, lt & 7
                if lw == 2:
                    llen, lo = _varint(layer, lo)
                    if lf == 1:
                        try:
                            name = layer[lo:lo + llen].decode("utf-8", errors="replace")
                        except Exception:
                            pass
                        break
                    lo += llen
                else:
                    lo = _skip(layer, lo, lw)
            yield name, layer
        else:
            off = _skip(tile_buf, off, wire)


def layer_breakdown(layer_buf):
    """Return (feature_count, feature_bytes, keys_bytes, values_bytes,
    geom_bytes) for a Layer submessage."""
    fcount = 0
    fbytes = 0
    kbytes = 0
    vbytes = 0
    gbytes = 0
    off = 0
    while off < len(layer_buf):
        tag, off = _varint(layer_buf, off)
        field, wire = tag >> 3, tag & 7
        if wire == 2:
            ln, off = _varint(layer_buf, off)
            if field == 2:  # Feature submessage
                fcount += 1
                fbytes += ln
                # Peek at geometry (feature.geometry field 4).
                fo = off
                fend = off + ln
                while fo < fend:
                    ft, fo = _varint(layer_buf, fo)
                    ff, fw = ft >> 3, ft & 7
                    if fw == 2:
                        fln, fo = _varint(layer_buf, fo)
                        if ff == 4:
                            gbytes += fln
                        fo += fln
                    else:
                        fo = _skip(layer_buf, fo, fw)
            elif field == 3:  # key string
                kbytes += ln + 1
            elif field == 4:  # Value submessage
                vbytes += ln + 1
            off += ln
        else:
            off = _skip(layer_buf, off, wire)
    return fcount, fbytes, kbytes, vbytes, gbytes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox",
        default="-73.94217484984556,45.040935177281284,"
                "-73.31660638748862,45.93918093916304")
    ap.add_argument("--minzoom", type=int, default=1)
    ap.add_argument("--maxzoom", type=int, default=14)
    ap.add_argument("--data", default="data",
                    help="directory holding *.mbtiles (default: ./data)")
    ap.add_argument("--topk", type=int, default=0,
                    help="if >0, also list the N heaviest tiles")
    args = ap.parse_args()

    w, s, e, n = (float(x) for x in args.bbox.split(","))
    mb_paths = sorted(pathlib.Path(args.data).glob("*.mbtiles"))
    if not mb_paths:
        print(f"no *.mbtiles in {args.data}")
        return

    print(f"bbox    : W={w} S={s} E={e} N={n}")
    print(f"zooms   : {args.minzoom}..{args.maxzoom}")
    print(f"mbtiles : {[p.name for p in mb_paths]}")
    print()

    total_tiles = 0
    total_wire = 0      # gzipped bytes on disk (= what's sent to client)
    total_raw = 0       # post-gunzip

    # per-layer totals across all zoom levels
    layer_agg = defaultdict(lambda: {
        "bytes": 0, "features": 0, "tiles": 0,
        "keys": 0, "values": 0, "geom": 0,
    })
    # per-zoom totals
    zoom_agg = defaultdict(lambda: {"tiles": 0, "wire": 0, "raw": 0})
    # per-layer per-zoom (for heatmap-style output)
    pz_agg = defaultdict(lambda: defaultdict(int))  # pz_agg[layer][z] = bytes
    heaviest = []  # (wire_size, z, x, y_tms, path)

    for path in mb_paths:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for z in range(args.minzoom, args.maxzoom + 1):
            x0, y0 = lonlat_to_tile(w, n, z)  # NW corner (y_xyz small)
            x1, y1 = lonlat_to_tile(e, s, z)  # SE corner (y_xyz large)
            if x1 < x0:
                x0, x1 = x1, x0
            if y1 < y0:
                y0, y1 = y1, y0
            N = 1 << z
            y_tms_min = N - 1 - y1
            y_tms_max = N - 1 - y0
            rows = db.execute(
                "SELECT tile_column, tile_row, tile_data FROM tiles "
                "WHERE zoom_level=? AND tile_column BETWEEN ? AND ? "
                "AND tile_row BETWEEN ? AND ?",
                (z, x0, x1, y_tms_min, y_tms_max),
            ).fetchall()
            for col, y_tms, blob in rows:
                if not blob:
                    continue
                wire_len = len(blob)
                try:
                    raw = gzip.decompress(blob)
                except OSError:
                    raw = blob
                raw_len = len(raw)
                total_tiles += 1
                total_wire += wire_len
                total_raw += raw_len
                zoom_agg[z]["tiles"] += 1
                zoom_agg[z]["wire"] += wire_len
                zoom_agg[z]["raw"] += raw_len
                if args.topk:
                    y_xyz = N - 1 - y_tms
                    heaviest.append((wire_len, z, col, y_xyz, path.name))
                try:
                    for name, layer in iter_layer_slices(raw):
                        fcount, fbytes, kbytes, vbytes, gbytes = layer_breakdown(layer)
                        layer_agg[name]["bytes"] += len(layer)
                        layer_agg[name]["features"] += fcount
                        layer_agg[name]["tiles"] += 1
                        layer_agg[name]["keys"] += kbytes
                        layer_agg[name]["values"] += vbytes
                        layer_agg[name]["geom"] += gbytes
                        pz_agg[name][z] += len(layer)
                except Exception as ex:
                    pass  # skip malformed tile; stats just lose one sample
        db.close()

    print(f"total tiles : {total_tiles:,}")
    print(f"total wire  : {fmt(total_wire)}   (gzipped, as served to client)")
    print(f"total raw   : {fmt(total_raw)}    (uncompressed)")
    if total_raw:
        print(f"gzip ratio  : {total_wire / total_raw:.1%}")
    print()

    print(f"{'zoom':>5s} {'tiles':>8s} {'wire':>10s} {'raw':>10s} {'avg wire':>10s}")
    for z in sorted(zoom_agg):
        za = zoom_agg[z]
        avg = za["wire"] / za["tiles"] if za["tiles"] else 0
        print(f"{z:>5d} {za['tiles']:>8,d} {fmt(za['wire']):>10s} "
              f"{fmt(za['raw']):>10s} {fmt(int(avg)):>10s}")
    print()

    # Per-layer summary sorted by descending total bytes.
    rows = sorted(layer_agg.items(), key=lambda kv: -kv[1]["bytes"])
    layer_total = sum(v["bytes"] for _, v in rows)
    print(f"{'layer':22s} {'bytes':>10s} {'%':>5s} {'feats':>10s} "
          f"{'keys':>9s} {'values':>9s} {'geom':>9s} {'tiles':>7s}")
    print("-" * 82)
    for name, v in rows:
        pct = v["bytes"] / layer_total * 100 if layer_total else 0
        print(f"{name:22s} {fmt(v['bytes']):>10s} {pct:>4.1f}% "
              f"{v['features']:>10,d} {fmt(v['keys']):>9s} "
              f"{fmt(v['values']):>9s} {fmt(v['geom']):>9s} {v['tiles']:>7,d}")
    print("-" * 82)
    print(f"{'total (layers)':22s} {fmt(layer_total):>10s}")
    print()

    # Zoom-layer breakdown for the heaviest layers.
    heavy_layers = [n for n, _ in rows[:8]]
    if heavy_layers:
        zooms = sorted(zoom_agg)
        print("per-zoom bytes for heaviest layers:")
        header = f"{'layer':22s}" + "".join(f"{f'z{z}':>10s}" for z in zooms)
        print(header)
        for name in heavy_layers:
            line = f"{name:22s}"
            for z in zooms:
                line += f"{fmt(pz_agg[name].get(z, 0)):>10s}"
            print(line)
        print()

    if args.topk:
        heaviest.sort(reverse=True)
        print(f"top {args.topk} heaviest tiles:")
        for wire_len, z, x, y_xyz, fname in heaviest[: args.topk]:
            print(f"  {fname} z{z}/{x}/{y_xyz}  {fmt(wire_len)}")


if __name__ == "__main__":
    main()
