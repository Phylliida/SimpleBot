#!/usr/bin/env python3
import json
import sqlite3
import sys
import time
from shapely.geometry import Point, LineString, shape
from shapely.strtree import STRtree


def count_lines(path):
    with open(path, "rb") as f:
        n = sum(1 for _ in f)
    return n


def progress(it, label, total=None, every=20000):
    start = time.time()
    n = 0
    for item in it:
        yield item
        n += 1
        if n % every == 0:
            elapsed = time.time() - start
            rate = n / elapsed if elapsed > 0 else 0
            bar = ""
            if total:
                pct = n / total * 100
                width = 24
                filled = int(width * n / total)
                bar = "[" + "#" * filled + "-" * (width - filled) + f"] {pct:5.1f}%"
                msg = f"  {label} {bar} {n:>10,}/{total:<10,} {rate:>7,.0f}/s  {elapsed:>5.1f}s"
            else:
                msg = f"  {label} {n:>10,} {rate:>7,.0f}/s  {elapsed:>5.1f}s"
            sys.stderr.write("\r\033[K" + msg)
            sys.stderr.flush()
    elapsed = time.time() - start
    sys.stderr.write(f"\r\033[K  {label} done — {n:,} features in {elapsed:.1f}s\n")
    sys.stderr.flush()

PRIORITY = [
    "amenity", "shop", "tourism", "leisure", "historic",
    "craft", "office", "public_transport", "railway", "highway",
]

EXTRA_FIELDS = [
    "addr:housenumber", "addr:street", "addr:city",
    "addr:county", "addr:state", "addr:country", "addr:postcode",
    "opening_hours",
    "phone", "contact:phone",
    "website", "contact:website",
    "wheelchair",
    "brand", "operator",
    "cuisine",
    "description",
    "wikipedia", "wikidata",
    "internet_access",
]

ADDR_MATCH_EPS = 0.0015     # ~160 m
INTERP_MATCH_EPS = 0.00045  # ~50 m
CITY_MATCH_EPS = 0.3        # ~30 km

PLACE_CLASSES = {"city", "town", "village", "suburb", "hamlet"}

# OSM admin_level -> POI address field. Common global conventions.
ADMIN_LEVEL_TO_FIELD = {
    "2": "addr:country",
    "4": "addr:state",
    "6": "addr:county",
    "8": "addr:city",
}


def center(feat):
    geom = feat.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Point" and len(coords) >= 2:
        return coords[0], coords[1]
    try:
        if gtype == "Polygon" and coords and coords[0]:
            ring = coords[0]
        elif gtype == "MultiPolygon" and coords and coords[0] and coords[0][0]:
            ring = coords[0][0]
        else:
            return None, None
        if not ring:
            return None, None
        sx = sum(p[0] for p in ring)
        sy = sum(p[1] for p in ring)
        n = len(ring)
        return sx / n, sy / n
    except Exception:
        return None, None


def iter_features(src):
    with open(src, "r", encoding="utf-8") as f:
        for line in f:
            line = line.lstrip("\x1e").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def interpolate_number(start, end, fraction, interp_type):
    try:
        s, e = int(start), int(end)
    except (ValueError, TypeError):
        return None
    raw = s + fraction * (e - s)
    if interp_type == "even":
        return str(round(raw / 2) * 2)
    if interp_type == "odd":
        return str(round((raw - 1) / 2) * 2 + 1)
    return str(round(raw))


def main(src, dst):
    sys.stderr.write(f"  counting features…"); sys.stderr.flush()
    total = count_lines(src)
    sys.stderr.write(f"\r\033[K  source has {total:,} features\n"); sys.stderr.flush()

    # Pass 1: collect sources for enrichment.
    addr_pts = []        # shapely Points for STRtree
    addr_props = []      # [{k: v, ...}]  parallel to addr_pts

    place_pts = []       # shapely Points
    place_names = []     # parallel

    interp_ways = []     # [{'coords': [...], 'street': str, 'type': str}]

    housenum_nodes = {}  # (round(lng,6), round(lat,6)) -> housenumber string

    boundary_geoms = []  # shapely Polygon/MultiPolygon
    boundary_meta = []   # [(admin_level, name)] parallel

    for feat in progress(iter_features(src), "pass 1 (collect sources)", total=total):
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")

        addr = {k: v for k, v in props.items() if k.startswith("addr:") and v}
        if addr:
            lng, lat = center(feat)
            if lng is not None:
                addr_pts.append(Point(lng, lat))
                addr_props.append(addr)

        if props.get("place") in PLACE_CLASSES:
            name = (props.get("name:latin") or props.get("name") or "").strip()
            if name:
                lng, lat = center(feat)
                if lng is not None:
                    place_pts.append(Point(lng, lat))
                    place_names.append(name)

        if (props.get("addr:interpolation")
                and gtype == "LineString"
                and len(geom.get("coordinates") or []) >= 2):
            coords = geom["coordinates"]
            interp_ways.append({
                "coords": coords,
                "street": props.get("addr:street", ""),
                "type": props["addr:interpolation"],
            })

        if (props.get("addr:housenumber")
                and gtype == "Point"
                and len(geom.get("coordinates") or []) >= 2):
            lng, lat = geom["coordinates"]
            housenum_nodes[(round(lng, 6), round(lat, 6))] = props["addr:housenumber"]

        if (props.get("boundary") == "administrative"
                and gtype in ("Polygon", "MultiPolygon")):
            al = str(props.get("admin_level", ""))
            if al in ADMIN_LEVEL_TO_FIELD:
                name = (props.get("name:latin") or props.get("name") or "").strip()
                if name:
                    try:
                        g = shape(geom)
                        if g.is_valid:
                            boundary_geoms.append(g)
                            boundary_meta.append((al, name))
                    except Exception:
                        pass

    # Resolve interpolation-way endpoints to house numbers via node lookup.
    resolved_interp = []
    interp_lines = []
    for w in interp_ways:
        s_key = (round(w["coords"][0][0], 6), round(w["coords"][0][1], 6))
        e_key = (round(w["coords"][-1][0], 6), round(w["coords"][-1][1], 6))
        s_num = housenum_nodes.get(s_key)
        e_num = housenum_nodes.get(e_key)
        if s_num is None or e_num is None:
            continue
        try:
            line = LineString(w["coords"])
            if not line.is_valid or line.length == 0:
                continue
        except Exception:
            continue
        resolved_interp.append({
            "start": s_num,
            "end": e_num,
            "street": w["street"],
            "type": w["type"],
            "line": line,
        })
        interp_lines.append(line)

    # Build spatial indexes.
    sys.stderr.write(
        f"  collected: addresses={len(addr_pts):,}  places={len(place_pts):,}  "
        f"interp_ways={len(interp_lines):,}  boundaries={len(boundary_geoms):,}\n"
    )
    sys.stderr.flush()
    t0 = time.time()
    sys.stderr.write("  building spatial indexes…"); sys.stderr.flush()
    addr_tree = STRtree(addr_pts) if addr_pts else None
    place_tree = STRtree(place_pts) if place_pts else None
    interp_tree = STRtree(interp_lines) if interp_lines else None
    boundary_tree = STRtree(boundary_geoms) if boundary_geoms else None
    sys.stderr.write(f"\r\033[K  spatial indexes built in {time.time() - t0:.1f}s\n")
    sys.stderr.flush()

    # Pass 2: write POIs, enriching each.
    db = sqlite3.connect(dst)
    db.execute("DROP TABLE IF EXISTS poi")
    db.execute("DROP TABLE IF EXISTS poi_rtree")
    db.execute("""CREATE TABLE poi(
        lng REAL, lat REAL, name TEXT, category TEXT, props TEXT
    )""")
    db.execute("CREATE INDEX idx_lng ON poi(lng)")
    db.execute("CREATE INDEX idx_lat ON poi(lat)")
    # Spatial index — bbox viewport queries use `poi_rtree` and JOIN back
    # to `poi` by rowid (populated in one pass at the end). Empty/sparse
    # regions go from ~400ms → ~1ms, dense regions ~3x faster.
    db.execute("""CREATE VIRTUAL TABLE poi_rtree USING rtree(
        id, minX, maxX, minY, maxY
    )""")

    counts = {
        "total": 0, "own_addr": 0,
        "nearby_addr": 0, "interp": 0,
        "pip": 0, "city_fallback": 0,
    }

    for feat in progress(iter_features(src), "pass 2 (enrich + write)", total=total):
        props = feat.get("properties") or {}
        name = (props.get("name:latin") or props.get("name") or "").strip()
        if not name:
            continue
        lng, lat = center(feat)
        if lng is None:
            continue

        category = ""
        for k in PRIORITY:
            if k in props:
                category = props[k]
                break

        extra = {k: props[k] for k in EXTRA_FIELDS if k in props and props[k]}
        has_own_addr = any(k.startswith("addr:") for k in extra)
        if has_own_addr:
            counts["own_addr"] += 1

        pt = Point(lng, lat)

        # 1. Nearby addressed feature — copy missing address fields.
        if addr_tree is not None:
            idx = addr_tree.nearest(pt)
            if addr_pts[idx].distance(pt) < ADDR_MATCH_EPS:
                nearby = addr_props[idx]
                for k, v in nearby.items():
                    if k in EXTRA_FIELDS and k not in extra:
                        extra[k] = v
                if any(k.startswith("addr:") for k in nearby):
                    counts["nearby_addr"] += 1

        # 2. Street interpolation — fill housenumber + street.
        if "addr:housenumber" not in extra and interp_tree is not None:
            best_idx, best_dist = None, INTERP_MATCH_EPS
            for idx in interp_tree.query(pt.buffer(INTERP_MATCH_EPS)):
                d = interp_lines[idx].distance(pt)
                if d < best_dist:
                    best_dist = d
                    best_idx = idx
            if best_idx is not None:
                w = resolved_interp[best_idx]
                fraction = w["line"].project(pt, normalized=True)
                num = interpolate_number(w["start"], w["end"], fraction, w["type"])
                if num:
                    extra["addr:housenumber"] = num
                    if w["street"] and "addr:street" not in extra:
                        extra["addr:street"] = w["street"]
                    counts["interp"] += 1

        # 3. Point-in-polygon boundaries — fill country/state/county/city.
        if boundary_tree is not None:
            added_city = False
            for idx in boundary_tree.query(pt):
                if boundary_geoms[idx].contains(pt):
                    al, bname = boundary_meta[idx]
                    field = ADMIN_LEVEL_TO_FIELD[al]
                    if field not in extra:
                        extra[field] = bname
                        if field == "addr:city":
                            added_city = True
            if added_city:
                counts["pip"] += 1

        # 4. Nearest-city fallback — last resort for addr:city.
        if "addr:city" not in extra and place_tree is not None:
            idx = place_tree.nearest(pt)
            if place_pts[idx].distance(pt) < CITY_MATCH_EPS:
                extra["addr:city"] = place_names[idx]
                counts["city_fallback"] += 1

        counts["total"] += 1
        db.execute(
            "INSERT INTO poi(lng, lat, name, category, props) VALUES (?, ?, ?, ?, ?)",
            (lng, lat, name, category,
             json.dumps(extra, ensure_ascii=False) if extra else ""),
        )

    # Populate the rtree from the now-inserted poi rows. Doing it in one
    # pass at the end is faster than maintaining the rtree as each row is
    # written (~120k inserts/sec vs ~40k).
    db.execute(
        "INSERT INTO poi_rtree(id, minX, maxX, minY, maxY) "
        "SELECT rowid, lng, lng, lat, lat FROM poi"
    )
    db.commit()
    db.close()

    print(
        f"  indexed {counts['total']} POIs — "
        f"own_addr:{counts['own_addr']} "
        f"nearby:{counts['nearby_addr']} "
        f"interp:{counts['interp']} "
        f"pip_city:{counts['pip']} "
        f"city_fallback:{counts['city_fallback']} "
        f"→ {dst}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: build-poi-db.py input.geojsonseq output.sqlite", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
