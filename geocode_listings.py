#!/usr/bin/env python3
"""Geocode Aisne listing locations via the French BAN API (base adresse nationale).

Free, no API key, direct egress (ZERO Webshare bandwidth cost).
Writes a `geo_cache` table into listings.db so the dashboard map can plot
listings without re-geocoding on every request.

Cache key: accent-stripped, postcode-stripped town name. When the source
location carries a postcode we pass it to BAN so "Braine" can't resolve to
the wrong département; otherwise we ask for several hits and prefer dept 02.

Usage:
    .venv/bin/python geocode_listings.py            # geocode new locations
    .venv/bin/python geocode_listings.py --report   # coverage report only
    .venv/bin/python geocode_listings.py --refresh  # re-geocode everything
"""
import argparse
import json
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

DB = "listings.db"
BAN = "https://api-adresse.data.gouv.fr/search/"
UA = "aisne-property-search/1.0 (personal investment research)"
SLEEP = 0.12  # polite: ~8 req/s max


class TransientBanError(Exception):
    """Network/5xx failure — the town must NOT be cached as a permanent miss."""


def norm_town(loc: str) -> str:
    """'Château-Thierry (02400)' -> 'chateau thierry'"""
    if not loc:
        return ""
    s = unicodedata.normalize("NFKD", loc)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(?\d{2,5}\)?", " ", s)
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def loc_postcode(loc: str) -> str:
    m = re.search(r"(\d{5})", loc or "")
    if m:
        return m.group(1)
    m = re.search(r"\((\d{2})\)", loc or "")
    return m.group(1) if m else ""


def ensure_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS geo_cache (
            loc_key   TEXT PRIMARY KEY,
            town      TEXT,
            lat       REAL,
            lon       REAL,
            citycode  TEXT,
            postcode  TEXT,
            depcode   TEXT,
            ban_score REAL,
            fetched_at TEXT
        )
        """
    )
    db.commit()


def ban_lookup(town: str, postcode: str) -> dict | None:
    """Query BAN for a municipality. Returns best hit or None.

    Raises TransientBanError on HTTP 5xx / network errors so the caller can
    leave the town UNCACHED and retry on a later run (caching a transient
    504 as a permanent miss would poison the cache forever).
    """
    params = {"q": town, "type": "municipality", "limit": "5"}
    if postcode and len(postcode) == 5:
        params["postcode"] = postcode
    url = BAN + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code >= 500 or e.code == 429:
            raise TransientBanError(f"HTTP {e.code}") from e
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TransientBanError(str(e)) from e
    feats = data.get("features") or []
    if not feats:
        return None
    # prefer Aisne (02) hits, then the highest BAN score
    def rank(f):
        p = f.get("properties", {})
        return (0 if p.get("depcode") == "02" else 1, -float(p.get("score") or 0))
    best = sorted(feats, key=rank)[0]
    p = best.get("properties", {})
    coords = (best.get("geometry") or {}).get("coordinates") or []
    if len(coords) != 2:
        return None
    return {
        "lat": coords[1], "lon": coords[0],
        "citycode": p.get("citycode"), "postcode": p.get("postcode"),
        "depcode": p.get("depcode"), "score": p.get("score"),
        "label": p.get("name"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="coverage report only")
    ap.add_argument("--refresh", action="store_true", help="re-geocode all")
    ap.add_argument("--limit", type=int, default=0, help="cap lookups (testing)")
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    ensure_table(db)

    rows = db.execute(
        "SELECT location, COUNT(*) FROM listings "
        "WHERE location IS NOT NULL AND location != '' GROUP BY location"
    ).fetchall()

    # build town -> (count, sample postcode)
    wanted: dict[str, dict] = {}
    for loc, n in rows:
        k = norm_town(loc)
        if not k:
            continue
        pc = loc_postcode(loc)
        e = wanted.setdefault(k, {"n": 0, "pc": ""})
        e["n"] += n
        if pc and not e["pc"]:
            e["pc"] = pc

    total_listings = sum(v["n"] for v in wanted.values())

    if args.report:
        cached = {r[0] for r in db.execute("SELECT loc_key FROM geo_cache")}
        have = [k for k in wanted if k in cached]
        miss = [k for k in wanted if k not in cached]
        print(f"distinct towns: {len(wanted)}  cached: {len(have)}  missing: {len(miss)}")
        cov = sum(wanted[k]["n"] for k in have)
        print(f"listing coverage: {cov}/{total_listings} "
              f"({100 * cov / total_listings:.1f}%)")
        if miss:
            print("missing sample:", ", ".join(sorted(miss)[:15]))
        return 0

    if args.refresh:
        db.execute("DELETE FROM geo_cache")
        db.commit()

    cached = {r[0]: r[1] for r in db.execute("SELECT loc_key, lat FROM geo_cache")}
    # towns never looked up, plus ones previously cached WITHOUT coordinates
    # (a definitive BAN miss, or a transient failure from an older run) — those
    # are cheap to re-ask, and leaving them out would hide a bad cache entry.
    todo = sorted(k for k in wanted if cached.get(k) is None)
    if args.limit:
        todo = todo[: args.limit]
    print(f"geocoding {len(todo)} town(s) via BAN "
          f"({len(cached) - sum(1 for v in cached.values() if v is None)} already resolved)\n",
          flush=True)

    ok = fail = skipped = 0
    for i, k in enumerate(todo, 1):
        try:
            hit = ban_lookup(k, wanted[k]["pc"])
        except TransientBanError as e:
            # leave UNCACHED so a later run retries — never poison the cache
            print(f"    ! transient BAN failure for {k!r} ({e}) — will retry later",
                  file=sys.stderr)
            skipped += 1
            time.sleep(0.6)
            continue
        if hit:
            db.execute(
                "INSERT OR REPLACE INTO geo_cache "
                "(loc_key, town, lat, lon, citycode, postcode, depcode, ban_score, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
                (k, hit.get("label") or k, hit["lat"], hit["lon"],
                 hit["citycode"], hit["postcode"], hit["depcode"], hit["score"]),
            )
            ok += 1
        else:
            db.execute(
                "INSERT OR REPLACE INTO geo_cache "
                "(loc_key, town, lat, lon, citycode, postcode, depcode, ban_score, fetched_at) "
                "VALUES (?,?,NULL,NULL,NULL,NULL,NULL,NULL,datetime('now'))",
                (k, k),
            )
            fail += 1
        if i % 50 == 0:
            db.commit()
            print(f"  {i}/{len(todo)}  (ok={ok} miss={fail})", flush=True)
        time.sleep(SLEEP)
    db.commit()

    total = db.execute("SELECT COUNT(*) FROM geo_cache").fetchone()[0]
    geo = db.execute("SELECT COUNT(*) FROM geo_cache WHERE lat IS NOT NULL").fetchone()[0]
    print(f"\ndone: {ok} geocoded, {fail} unmatched, {skipped} transient-skipped; "
          f"cache now {total} rows ({geo} with coordinates, "
          f"{100 * geo / total:.0f}%)")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
