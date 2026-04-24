#!/usr/bin/env python3
"""Streaming GTFS ingester: download → ingest → delete, one feed at a time.

Usage:
  ingest-gtfs.py <schedule.sqlite> --feeds feeds.tsv
  ingest-gtfs.py <schedule.sqlite> --single <slug> <url>

feeds.tsv format (tab-separated): slug <TAB> url <TAB> optional_name

Resumable: skips any slug already in feed_meta. Failed downloads are logged
and skipped; rerun to retry. Peak disk usage is bounded by the largest
single GTFS zip (~100-300 MB for major metros).
"""
import argparse
import csv
import io
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile


BUILD_SCRIPT = pathlib.Path(__file__).parent / "build-schedule-db.py"
VALIDATE_SCRIPT = pathlib.Path(__file__).parent / "validate-gtfs.py"


def read_feeds_tsv(path):
    feeds = []
    with open(path, encoding="utf-8") as f:
        for line_no, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                sys.stderr.write(f"  skip line {line_no}: need <slug>\\t<url> got {line!r}\n")
                continue
            slug = parts[0].strip()
            url = parts[1].strip()
            name = parts[2].strip() if len(parts) > 2 else ""
            fallback = parts[3].strip() if len(parts) > 3 else ""
            if slug and url:
                feeds.append((slug, url, name, fallback))
    return feeds


def already_ingested(db_path, slug):
    if not os.path.exists(db_path):
        return False
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        try:
            row = conn.execute(
                "SELECT 1 FROM feed_meta WHERE slug=?", (slug,)
            ).fetchone()
            return row is not None
        except sqlite3.OperationalError:
            return False  # table may not exist yet


def download(url, dest):
    """Stream download into dest. Raises on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "creature-collect/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f, length=1024 * 1024)


def try_download(url, dest):
    """Download + sanity-check that we actually got a zip archive. Many GTFS
    mirrors return 200 OK with an HTML error page or an empty body; a zip-
    magic-byte check catches these before we hand the file to the validator.
    Returns None on success, or a short reason string on failure."""
    try:
        download(url, dest)
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError) as e:
        return f"download error {e}"
    try:
        if os.path.getsize(dest) < 22:  # minimum valid zip is ~22 bytes
            return "download returned near-empty body"
        with open(dest, "rb") as f:
            sig = f.read(4)
        if sig[:2] != b"PK":
            return "download body is not a zip file"
    except OSError as e:
        return f"post-download check failed: {e}"
    return None


def presort_stop_times_in_zip(zip_path):
    """If stop_times.txt isn't already grouped by trip_id, rewrite the zip
    with the rows sorted by (trip_id, stop_sequence). `build-schedule-db.py`
    streams stop_times in source order and flushes per trip_id transition,
    so unsorted input produces duplicate pattern rows — this pre-sort pass
    makes those feeds ingestable without any changes downstream.
    """
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        return  # validator will flag
    try:
        try:
            raw = z.read("stop_times.txt")
        except KeyError:
            return
        # Fast path: scan once. If no trip_id re-appears after its run, already sorted.
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        seen = set(); current = None; sorted_ = True
        for row in reader:
            tid = row.get("trip_id", "")
            if tid != current:
                if tid in seen:
                    sorted_ = False
                    break
                seen.add(tid); current = tid
        if sorted_:
            return
        sys.stderr.write(f"[presort] stop_times.txt not grouped by trip_id — rewriting\n")
        # Re-parse and sort. Keep other files byte-identical.
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        rows = list(reader)
        fieldnames = reader.fieldnames or []
        def _seq(v):
            try: return int(v)
            except (TypeError, ValueError): return 0
        rows.sort(key=lambda r: (r.get("trip_id", ""),
                                 _seq(r.get("stop_sequence", 0))))
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        sorted_bytes = out.getvalue().encode("utf-8")
        other_names = [n for n in z.namelist() if n != "stop_times.txt"]
        others = {n: z.read(n) for n in other_names}
    finally:
        z.close()
    tmp = zip_path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        zout.writestr("stop_times.txt", sorted_bytes)
        for n, data in others.items():
            zout.writestr(n, data)
    os.replace(tmp, zip_path)


def ingest_one(db_path, slug, url, name, tmp_root, skip_validate=False, fallback=""):
    if already_ingested(db_path, slug):
        sys.stderr.write(f"[skip] {slug} already ingested\n")
        return "skipped"
    label = f"{slug}" + (f" ({name})" if name else "")
    sys.stderr.write(f"[fetch] {label}\n"); sys.stderr.flush()
    t0 = time.time()

    with tempfile.NamedTemporaryFile(
        suffix=".zip", dir=tmp_root, delete=False
    ) as tmp:
        tmp_path = tmp.name
    try:
        err = try_download(url, tmp_path)
        if err and fallback and fallback != url:
            sys.stderr.write(f"[retry] {label}: {err} — trying fallback URL\n")
            err = try_download(fallback, tmp_path)
        if err:
            sys.stderr.write(f"[fail] {label}: {err}\n")
            return "download_failed"
        zip_size = os.path.getsize(tmp_path)
        try:
            presort_stop_times_in_zip(tmp_path)
        except Exception as e:
            sys.stderr.write(f"[warn] {label}: presort failed ({e}) — continuing\n")
        sys.stderr.write(
            f"[validate] {label}  ({zip_size / 1024 / 1024:.1f} MB, "
            f"{time.time() - t0:.1f}s download)\n"
        )
        if not skip_validate:
            vproc = subprocess.run(
                [sys.executable, str(VALIDATE_SCRIPT), tmp_path],
                stderr=subprocess.STDOUT,
                stdout=subprocess.PIPE,
            )
            sys.stderr.write(vproc.stdout.decode("utf-8", errors="replace"))
            if vproc.returncode >= 2:
                sys.stderr.write(f"[fail] {label}: validation errors — skipping. "
                                 f"Pass --skip-validate to force ingest anyway.\n")
                return "validation_failed"
        sys.stderr.write(f"[ingest] {label}\n"); sys.stderr.flush()
        proc = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT), slug, tmp_path, db_path],
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
        )
        sys.stderr.write(proc.stdout.decode("utf-8", errors="replace"))
        sys.stderr.flush()
        if proc.returncode != 0:
            sys.stderr.write(f"[fail] {label}: build exited {proc.returncode}\n")
            return "ingest_failed"
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE feed_meta SET url=? WHERE slug=?", (url, slug))
            conn.commit()
        return "ok"
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("schedule_db")
    ap.add_argument("--feeds", help="tab-separated file of slug<TAB>url<TAB>[name]")
    ap.add_argument("--single", nargs=2, metavar=("SLUG", "URL"),
                    help="ingest one feed directly")
    ap.add_argument("--tmp", default=None, help="temp directory for zip downloads")
    ap.add_argument("--skip-validate", action="store_true",
                    help="skip format validation before ingesting")
    args = ap.parse_args()

    if not args.feeds and not args.single:
        ap.error("need --feeds FILE or --single SLUG URL")

    feeds = []
    if args.single:
        feeds.append((args.single[0], args.single[1], "", ""))
    if args.feeds:
        feeds.extend(read_feeds_tsv(args.feeds))
    if not feeds:
        sys.stderr.write("no feeds to process\n")
        return 1

    tmp_root = args.tmp or tempfile.gettempdir()

    counts = {"ok": 0, "skipped": 0, "download_failed": 0,
              "validation_failed": 0, "ingest_failed": 0}
    for i, (slug, url, name, fallback) in enumerate(feeds, 1):
        sys.stderr.write(f"\n=== [{i}/{len(feeds)}] {slug} ===\n")
        try:
            result = ingest_one(args.schedule_db, slug, url, name, tmp_root,
                                skip_validate=args.skip_validate,
                                fallback=fallback)
        except KeyboardInterrupt:
            sys.stderr.write("interrupted\n")
            break
        except Exception as e:
            sys.stderr.write(f"[fail] {slug}: unexpected error {e}\n")
            result = "ingest_failed"
        counts[result] = counts.get(result, 0) + 1

    sys.stderr.write(
        f"\n=== done: ok={counts['ok']}, skipped={counts['skipped']}, "
        f"download_failed={counts['download_failed']}, "
        f"validation_failed={counts['validation_failed']}, "
        f"ingest_failed={counts['ingest_failed']} ===\n"
    )
    bad = counts["download_failed"] + counts["validation_failed"] + counts["ingest_failed"]
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
