#!/usr/bin/env python3
"""Filter the Mobility Database catalog to a country and emit a feeds.tsv.

Usage: get-gtfs-catalog.py --country CA [--output feeds-ca.tsv] [--catalog PATH]
       [--use-latest]

The --use-latest flag writes Mobility Database's mirror URL (urls.latest)
instead of the direct agency URL (urls.direct_download). The MD mirror is
stabler but adds indirection.
"""
import argparse
import csv
import sys
import urllib.request


DEFAULT_CATALOG_URL = "https://files.mobilitydatabase.org/feeds_v2.csv"


def fetch_catalog(url_or_path):
    if url_or_path.startswith(("http://", "https://")):
        sys.stderr.write(f"fetching catalog from {url_or_path}\n")
        with urllib.request.urlopen(url_or_path, timeout=180) as resp:
            return resp.read().decode("utf-8-sig")
    with open(url_or_path, encoding="utf-8-sig") as f:
        return f.read()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--country", required=True,
                    help="ISO 3166-1 alpha-2 code, e.g. CA, US, GB")
    ap.add_argument("--output", default="-",
                    help="output file (default stdout)")
    ap.add_argument("--catalog", default=DEFAULT_CATALOG_URL,
                    help="catalog CSV URL or local path")
    ap.add_argument("--use-latest", action="store_true",
                    help="prefer urls.latest (MD mirror) over urls.direct_download")
    ap.add_argument("--include-auth", action="store_true",
                    help="include feeds that require API keys (default: skip)")
    args = ap.parse_args()

    text = fetch_catalog(args.catalog)
    reader = csv.DictReader(text.splitlines())
    kept = []
    for row in reader:
        if row.get("location.country_code") != args.country: continue
        if row.get("data_type") != "gtfs": continue
        if row.get("status") != "active": continue
        if row.get("redirect.id"): continue
        auth = row.get("urls.authentication_type") or "0"
        if auth != "0" and not args.include_auth: continue
        direct = (row.get("urls.direct_download") or "").strip()
        latest = (row.get("urls.latest") or "").strip()
        if args.use_latest:
            url, fallback = latest or direct, ""
        else:
            url, fallback = direct or latest, latest if direct and latest and latest != direct else ""
        if not url: continue
        slug = row.get("id", "").strip()
        if not slug: continue
        provider = (row.get("provider") or "").strip()
        subdiv = (row.get("location.subdivision_name") or "").strip()
        muni = (row.get("location.municipality") or "").strip()
        label_parts = [p for p in (provider, muni, subdiv) if p]
        name = " / ".join(label_parts) or slug
        kept.append((slug, url, name, fallback))

    out = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8")
    try:
        for slug, url, name, fallback in kept:
            # TSV: slug <TAB> url <TAB> name <TAB> fallback_url (MD mirror).
            # ingest-gtfs.py retries with the fallback when the direct URL
            # returns a non-zip body or an HTTP error.
            out.write(f"{slug}\t{url}\t{name}\t{fallback}\n")
    finally:
        if out is not sys.stdout: out.close()
    sys.stderr.write(f"wrote {len(kept)} feeds for country={args.country}\n")


if __name__ == "__main__":
    main()
