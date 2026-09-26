#!/usr/bin/env python3
"""Fill missing listing descriptions from the detail page (direct egress).

Why: description coverage is uneven per source — lesiteimmo/paruvendu carry the
description on the search card (100%), while IAD's cards carry none at all
(0/589) and FNAIM's are mostly empty (42%). Descriptions are what make a listing
evaluable (works needed, tenancy status, DPE commentary), so the gaps matter.

Bandwidth: these are DIRECT fetches (no Webshare), so the run is free in proxy
terms — IAD and FNAIM both serve the description server-side.
SeLoger is skipped by default: it 403s direct requests, so its descriptions can
only come from the sweep's authenticated BFF cards (already 78% covered).

Extraction order per page: JSON-LD "description" -> og:description ->
meta[name=description] -> <p class=description>. The FNAIM JSON-LD holds the
PORTAL boilerplate rather than the listing, so for fnaim the meta description is
preferred — see _pick().
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import html
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent / "listings.db"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Sources that serve the description in server-side HTML on a direct fetch.
# century21 added 2026-09-26: its SEARCH cards carry only a truncated teaser, but
# the detail page exposes the full text via og:description/meta (verified, 545
# chars) — and that text names the asset ("immeuble de rapport", "local
# commercial", "3 appartements"), which is what the categoriser needs.
DETAIL_SOURCES = ("iad", "fnaim", "century21")


def _clean(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _pick(body: str, source: str) -> str:
    """Best available description for this source, cleaned."""
    meta = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{40,})',
                     body, re.I)
    meta_v = _clean(meta.group(1)) if meta else ""
    og = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']{40,})',
                   body, re.I)
    og_v = _clean(og.group(1)) if og else ""
    ld = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.){40,}?)"', body)
    ld_v = ""
    if ld:
        try:
            ld_v = _clean(json.loads(f'"{ld.group(1)}"'))
        except json.JSONDecodeError:
            ld_v = _clean(ld.group(1))

    # FNAIM's JSON-LD description is the portal's own blurb, not the listing's.
    if source == "fnaim":
        for cand in (meta_v, og_v, ld_v):
            if cand:
                return cand
        return ""
    for cand in (ld_v, og_v, meta_v):
        if cand:
            return cand
    return ""


def fetch_desc(url: str, source: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(700_000).decode("utf-8", "replace")
    return _pick(body, source)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", nargs="+", default=list(DETAIL_SOURCES))
    ap.add_argument("--limit", type=int, default=0, help="0 = all missing")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--min-len", type=int, default=40)
    # Enrich mode: the default selection only fills EMPTY descriptions, so a
    # truncated card teaser is never improved — which is why 30% of century21
    # rows classify as 'unclassified' despite being 100% "described". This
    # refetches short descriptions and keeps the longer of the two.
    ap.add_argument("--enrich-below", type=int, default=0,
                    help="also refetch descriptions shorter than N chars (0 = off)")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    ph = ",".join("?" * len(args.sources))
    sql = (f"SELECT url, source, title FROM listings "
           f"WHERE status='active' AND source IN ({ph}) "
           f"AND (description IS NULL OR description=''")
    if args.enrich_below:
        sql += f" OR LENGTH(description) < {int(args.enrich_below)}"
    sql += ")"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = con.execute(sql, args.sources).fetchall()
    print(f"missing descriptions: {len(rows)} row(s) across {', '.join(args.sources)}",
          flush=True)
    if not rows:
        return 0

    ok = fail = short = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_desc, r["url"], r["source"]): r for r in rows}
        for f in cf.as_completed(futs):
            r = futs[f]
            try:
                desc = f.result()
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                    OSError) as e:
                fail += 1
                print(f"  ERR  {r['source']:<6} {type(e).__name__}: {str(e)[:50]}",
                      flush=True)
                continue
            if len(desc) < args.min_len:
                short += 1
                print(f"  thin {r['source']:<6} {(r['title'] or '')[:34]:<34} "
                      f"(got {len(desc)} chars)", flush=True)
                continue
            prev_len = con.execute(
                "SELECT LENGTH(COALESCE(description,'')) FROM listings WHERE url=?",
                (r["url"],)).fetchone()[0] or 0
            if len(desc) <= prev_len:
                # Never shorten: a shorter fetch is a worse page (consent wall,
                # partial render), not new information.
                print(f"  keep {r['source']:<6} existing {prev_len} >= fetched "
                      f"{len(desc)}", flush=True)
                continue
            con.execute("UPDATE listings SET description=? WHERE url=?", (desc, r["url"]))
            con.execute("INSERT INTO listing_events (url, source, observed_at, event, note)"
                        " VALUES (?,?,?,?,?)",
                        (r["url"], r["source"], now, "description_filled",
                         f"{len(desc)} chars from detail page"))
            ok += 1
            if ok % 25 == 0:
                con.commit()
                print(f"  ... {ok} filled", flush=True)
    con.commit()

    print(f"\nfilled={ok}  thin(no real description)={short}  failed={fail}")
    print("\n=== description coverage now ===")
    for r in con.execute("""SELECT source, COUNT(*) n,
                                   SUM(CASE WHEN description IS NOT NULL AND description<>''
                                       THEN 1 ELSE 0 END) wd
                            FROM listings WHERE status='active' GROUP BY 1 ORDER BY 2 DESC"""):
        pct = 100 * r["wd"] / r["n"] if r["n"] else 0
        print(f"  {r['source']:<11} {r['wd']}/{r['n']} ({pct:.0f}%)")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
