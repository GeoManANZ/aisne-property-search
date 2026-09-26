#!/usr/bin/env python3
"""Crawl century21.fr (DIRECT egress — no proxy, no captcha, zero metered cost)
and ingest the result into listings.db, reporting rejection reasons.

Rejection reasons are printed because a silent "rejected=N" is how a whole source
can vanish from a pipeline while every run still looks healthy.
"""
import collections
import json
from pathlib import Path

from french_property_parsers import ladder
from listings_db import ListingsDB

r = ladder(sources=["century21"], max_listings_per_source=300, delay_s=1.0)
print(f"crawl: consolidated={r['consolidated_count']}  log={r.get('log_path')}")

log_path = r.get("log_path")
scan_dir = Path(log_path).parent if log_path else None
cons = (scan_dir / "consolidated_listings.json") if scan_dir else None
if not cons or not cons.exists():
    raise SystemExit("no consolidated_listings.json produced")
items = json.loads(cons.read_text(encoding="utf-8"))

rej_log: list = []
db = ListingsDB("listings.db")
acc, rej = db.bulk_upsert_validated(items, reject_log=rej_log)
print(f"INGEST accepted={acc} rejected={rej}")
if rej_log:
    c = collections.Counter(str(x)[:78] for x in rej_log[:800])
    for reason, n in c.most_common(6):
        print(f"   reject {n:3d}x {reason}")

print(f"DB total        : {db.count()}")
print("per source      :", dict(db.conn.execute(
    "SELECT source, COUNT(*) FROM listings GROUP BY source ORDER BY 2 DESC").fetchall()))
print("C21 rows        :", db.conn.execute(
    "SELECT COUNT(*) FROM listings WHERE source='century21'").fetchone()[0])
print("C21 in criteria :", db.conn.execute(
    "SELECT COUNT(*) FROM listings WHERE status='active' AND source='century21' "
    "AND surface_m2 BETWEEN 150 AND 1500 AND price_eur BETWEEN 10000 AND 220000"
).fetchone()[0])

print("\n=== cheapest C21 candidates inside the criteria ===")
for row in db.conn.execute(
        "SELECT price_eur, surface_m2, ROUND(price_eur/surface_m2), location, title, url "
        "FROM listings WHERE status='active' AND source='century21' "
        "AND surface_m2 BETWEEN 150 AND 1500 AND price_eur BETWEEN 10000 AND 220000 "
        "ORDER BY 3 LIMIT 6"):
    print(f"  €{row[0]:<8,} {row[1]:<7} m²  €{row[2]:<6}/m²  {(row[3] or '')[:20]:20s} "
          f"{(row[4] or '')[:26]}")
    print(f"      {row[5]}")
db.close()
