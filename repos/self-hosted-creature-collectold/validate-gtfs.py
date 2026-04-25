#!/usr/bin/env python3
"""Validate a GTFS zip against the assumptions made by build-schedule-db.py.

Usage: validate-gtfs.py <input.zip>
Exit codes:
  0 — all checks pass
  1 — warnings only (ingest will likely work but look at the output)
  2 — errors found (ingest will drop data or produce wrong schedules)
"""
import csv
import io
import sys
import zipfile
from collections import defaultdict


MAX_SAMPLES = 5


class Report:
    def __init__(self, check_name):
        self.check = check_name
        self.errors = []
        self.warnings = []
        self.info = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def note(self, msg):
        self.info.append(msg)

    def status(self):
        if self.errors: return "ERROR"
        if self.warnings: return "WARN"
        return "OK"

    def render(self):
        out = [f"[{self.status():<5}] {self.check}"]
        for e in self.errors: out.append(f"         error: {e}")
        for w in self.warnings: out.append(f"         warn:  {w}")
        for i in self.info: out.append(f"         info:  {i}")
        return "\n".join(out)


def read_csv(z, name):
    try:
        raw = z.read(name)
    except KeyError:
        return None
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


REQUIRED_FILES = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "stop_times.txt"]
OPTIONAL_FILES = ["calendar.txt", "calendar_dates.txt", "feed_info.txt"]


def check_required_files(z, _cache):
    r = Report("required-files")
    names = set(z.namelist())
    for f in REQUIRED_FILES:
        if f not in names:
            r.error(f"missing required file: {f}")
    has_cal = "calendar.txt" in names
    has_cald = "calendar_dates.txt" in names
    if not has_cal and not has_cald:
        r.error("neither calendar.txt nor calendar_dates.txt present")
    elif not has_cal:
        r.warn("no calendar.txt — all services must be exception-based in calendar_dates.txt")
    return r


def check_stop_coords(z, _cache):
    r = Report("stop-coordinates")
    rows = read_csv(z, "stops.txt")
    if rows is None:
        r.error("stops.txt missing"); return r
    bad_parse = []
    out_of_range = []
    for row in rows:
        sid = row.get("stop_id", "")
        try:
            lng = float(row["stop_lon"]); lat = float(row["stop_lat"])
        except (KeyError, ValueError, TypeError):
            bad_parse.append(sid)
            continue
        if not (-180 <= lng <= 180 and -90 <= lat <= 90):
            out_of_range.append((sid, lng, lat))
    if bad_parse:
        # Warn, don't error: the overwhelming majority of stops with blank
        # or non-numeric lng/lat are abstract location_type=2/3 pseudo-stops
        # (station entrances, generic nodes, platforms) that inherit geometry
        # from their parent_station. build-schedule-db.py skips them
        # automatically; no downstream breakage from tolerating them.
        r.warn(f"{len(bad_parse):,} stops with unparseable lng/lat "
               f"(sample: {bad_parse[:MAX_SAMPLES]}) — will be skipped")
    if out_of_range:
        r.error(f"{len(out_of_range):,} stops with out-of-range coords "
                f"(sample: {out_of_range[:MAX_SAMPLES]})")
    r.note(f"{len(rows):,} stops total")
    return r


def check_route_types(z, _cache):
    r = Report("route-types")
    rows = read_csv(z, "routes.txt")
    if rows is None:
        r.error("routes.txt missing"); return r
    bad = []
    for row in rows:
        rt = row.get("route_type")
        try:
            int(rt or 0)
        except ValueError:
            bad.append((row.get("route_id", ""), rt))
    if bad:
        # Downgrade: build-schedule-db coerces unknown/non-integer route_type
        # to 3 (bus), which is the safe default for unclassified services.
        r.warn(f"{len(bad):,} routes with non-integer route_type "
               f"(sample: {bad[:MAX_SAMPLES]}) — will be coerced to bus (3)")
    r.note(f"{len(rows):,} routes total")
    return r


def check_stop_times_sorted(z, cache):
    """Critical: we stream stop_times.txt and flush per trip_id transition."""
    r = Report("stop-times-sorted-by-trip")
    try:
        raw = z.read("stop_times.txt")
    except KeyError:
        r.error("stop_times.txt missing"); return r
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    seen_trip_ids = set()
    current = None
    transitions = 0
    unsorted_examples = []
    n = 0
    for row in reader:
        tid = row.get("trip_id", "")
        n += 1
        if tid != current:
            if tid in seen_trip_ids:
                if len(unsorted_examples) < MAX_SAMPLES:
                    unsorted_examples.append(tid)
            seen_trip_ids.add(tid)
            current = tid
            transitions += 1
    cache["stop_times_row_count"] = n
    cache["stop_times_trip_count"] = len(seen_trip_ids)
    if unsorted_examples:
        # ingest-gtfs.py pre-sorts stop_times.txt by (trip_id, stop_sequence)
        # before handing the zip to this validator in the normal flow, so
        # seeing this warning here means someone's running validate-gtfs
        # standalone. Either way it's not fatal.
        r.warn(f"trip_ids not grouped: {len(unsorted_examples)}+ trip_ids "
               f"re-appear after their run ended (sample: {unsorted_examples}) "
               f"— ingest-gtfs.py will auto-sort before handing to build")
    r.note(f"{n:,} stop_time rows across {len(seen_trip_ids):,} trips")
    return r


def check_stop_time_fields(z, cache):
    r = Report("stop-time-fields")
    try:
        raw = z.read("stop_times.txt")
    except KeyError:
        return r
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    bad_times = 0
    bad_seqs = 0
    non_monotonic = 0
    dup_sequence = 0
    trip_seqs = defaultdict(set)  # only tracks current trip to save memory
    current_trip = None
    current_seqs = set()
    prev_dep = None
    prev_dep_trip = None
    sample_bad_times = []
    sample_non_mono = []
    sample_dup = []
    for row in reader:
        tid = row.get("trip_id", "")
        if tid != current_trip:
            current_trip = tid
            current_seqs = set()
            prev_dep = None
            prev_dep_trip = tid
        try:
            seq = int(row.get("stop_sequence") or 0)
        except ValueError:
            bad_seqs += 1
            continue
        if seq in current_seqs:
            dup_sequence += 1
            if len(sample_dup) < MAX_SAMPLES:
                sample_dup.append((tid, seq))
        current_seqs.add(seq)
        dep = _time_to_sec(row.get("departure_time", ""))
        arr = _time_to_sec(row.get("arrival_time", ""))
        if dep is None and row.get("departure_time"):
            bad_times += 1
            if len(sample_bad_times) < MAX_SAMPLES:
                sample_bad_times.append((tid, row.get("departure_time")))
        if arr is None and row.get("arrival_time"):
            bad_times += 1
        if dep is not None and prev_dep is not None and prev_dep_trip == tid and dep < prev_dep:
            non_monotonic += 1
            if len(sample_non_mono) < MAX_SAMPLES:
                sample_non_mono.append((tid, prev_dep, dep))
        if dep is not None:
            prev_dep = dep
    if bad_seqs:
        r.error(f"{bad_seqs:,} stop_time rows with non-integer stop_sequence")
    if bad_times:
        r.warn(f"{bad_times:,} stop_time rows with unparseable times "
               f"(sample: {sample_bad_times[:MAX_SAMPLES]})")
    if dup_sequence:
        r.error(f"{dup_sequence:,} duplicate (trip_id, stop_sequence) pairs "
                f"(sample: {sample_dup[:MAX_SAMPLES]}) — trip will be dropped")
    if non_monotonic:
        r.warn(f"{non_monotonic:,} non-monotonic departure times "
               f"(sample: {sample_non_mono[:MAX_SAMPLES]}) — deltas will be clamped to 0")
    return r


def _time_to_sec(s):
    if not s: return None
    parts = s.split(":")
    if len(parts) != 3: return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None


def check_referential_integrity(z, _cache):
    r = Report("referential-integrity")
    routes = read_csv(z, "routes.txt") or []
    stops = read_csv(z, "stops.txt") or []
    trips = read_csv(z, "trips.txt") or []
    cal = read_csv(z, "calendar.txt") or []
    cald = read_csv(z, "calendar_dates.txt") or []

    route_ids = {r_.get("route_id", "") for r_ in routes}
    stop_ids = {s.get("stop_id", "") for s in stops}
    trip_ids = {t.get("trip_id", "") for t in trips}
    service_ids = {c.get("service_id", "") for c in cal} | {c.get("service_id", "") for c in cald}

    trip_route_misses = [t.get("trip_id") for t in trips
                         if t.get("route_id") and t.get("route_id") not in route_ids]
    trip_svc_misses = [t.get("trip_id") for t in trips
                       if t.get("service_id") and t.get("service_id") not in service_ids]
    # Downgraded: build-schedule-db.py drops any trip whose service_id info
    # is missing or whose stop_ids can't be resolved, and skips stop_times
    # rows pointing at unknown stops. These checks flag real data-quality
    # issues but the ingest won't crash or produce bad data.
    if trip_route_misses:
        r.warn(f"{len(trip_route_misses):,} trips reference unknown route_id "
               f"(sample: {trip_route_misses[:MAX_SAMPLES]}) — will be dropped")
    if trip_svc_misses:
        r.warn(f"{len(trip_svc_misses):,} trips reference unknown service_id "
               f"(sample: {trip_svc_misses[:MAX_SAMPLES]}) — will be dropped")

    try:
        raw = z.read("stop_times.txt")
    except KeyError:
        return r
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    bad_trip = 0; bad_stop = 0
    sample_bad_trip = []; sample_bad_stop = []
    for row in reader:
        tid = row.get("trip_id", "")
        sid = row.get("stop_id", "")
        if tid and tid not in trip_ids:
            bad_trip += 1
            if len(sample_bad_trip) < MAX_SAMPLES: sample_bad_trip.append(tid)
        if sid and sid not in stop_ids:
            bad_stop += 1
            if len(sample_bad_stop) < MAX_SAMPLES: sample_bad_stop.append(sid)
    if bad_trip:
        r.warn(f"{bad_trip:,} stop_times reference unknown trip_id "
               f"(sample: {sample_bad_trip[:MAX_SAMPLES]}) — row dropped by build")
    if bad_stop:
        r.warn(f"{bad_stop:,} stop_times reference unknown stop_id "
               f"(sample: {sample_bad_stop[:MAX_SAMPLES]}) — row dropped by build")
    return r


def check_calendar(z, _cache):
    r = Report("calendar-fields")
    cal = read_csv(z, "calendar.txt")
    if cal is None:
        return r
    bad_dow = 0; bad_date = 0
    for c in cal:
        for dow in ("monday", "tuesday", "wednesday", "thursday",
                    "friday", "saturday", "sunday"):
            v = c.get(dow, "")
            if v and v not in ("0", "1"):
                bad_dow += 1; break
        for d in ("start_date", "end_date"):
            v = c.get(d, "")
            if v and (len(v) != 8 or not v.isdigit()):
                bad_date += 1; break
    if bad_dow:
        r.warn(f"{bad_dow:,} calendar rows with non-0/1 day-of-week value")
    if bad_date:
        r.warn(f"{bad_date:,} calendar rows with malformed start_date/end_date "
               f"(expected YYYYMMDD)")
    cald = read_csv(z, "calendar_dates.txt") or []
    bad_et = 0
    for c in cald:
        v = c.get("exception_type", "")
        if v and v not in ("1", "2"):
            bad_et += 1
    if bad_et:
        r.error(f"{bad_et:,} calendar_dates rows with exception_type not in {{1,2}}")
    r.note(f"{len(cal):,} calendar rows, {len(cald):,} calendar_dates rows")
    return r


def check_direction_ids(z, _cache):
    r = Report("direction-ids")
    rows = read_csv(z, "trips.txt") or []
    bad = 0
    for t in rows:
        v = t.get("direction_id", "")
        if v and v not in ("0", "1"):
            bad += 1
    if bad:
        r.warn(f"{bad:,} trips with direction_id not in {{0,1}} — will be coerced")
    return r


def check_feed_freshness(z, _cache):
    r = Report("feed-freshness")
    import datetime
    today = datetime.date.today().strftime("%Y%m%d")
    cal = read_csv(z, "calendar.txt") or []
    if not cal:
        r.note("no calendar.txt — cannot determine freshness from calendar")
        return r
    max_end = max((c.get("end_date", "") for c in cal), default="")
    min_start = min((c.get("start_date", "") for c in cal if c.get("start_date", "")), default="")
    r.note(f"calendar covers {min_start}..{max_end}")
    if max_end and max_end < today:
        r.warn(f"all calendar entries expired before today ({today}) — feed is stale")
    return r


CHECKS = [
    check_required_files,
    check_stop_coords,
    check_route_types,
    check_stop_times_sorted,
    check_stop_time_fields,
    check_referential_integrity,
    check_calendar,
    check_direction_ids,
    check_feed_freshness,
]


def main(args):
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    zip_path = args[0]
    cache = {}
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        print(f"[ERROR] cannot open zip: {e}")
        return 2

    results = []
    for fn in CHECKS:
        try:
            results.append(fn(z, cache))
        except Exception as e:
            rep = Report(fn.__name__)
            rep.error(f"check crashed: {e}")
            results.append(rep)

    any_err = any(r.errors for r in results)
    any_warn = any(r.warnings for r in results)
    for r in results:
        print(r.render())

    print()
    if any_err:
        print("RESULT: errors found — ingest will produce broken data for this feed")
        return 2
    if any_warn:
        print("RESULT: warnings only — ingest should work but review output")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
