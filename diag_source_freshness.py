#!/usr/bin/env python3
"""Diagnostic: are ALL sources actually being re-scraped, or just ParuVendu?

Distinguishes three different things that are easy to conflate:
  * last_seen  — bumped only when a row is re-upserted
  * last_check — bumped on EVERY upsert touch
  * first_seen — a genuinely NEW listing (new ≠ re-scraped)

Also reads the scan directories on disk, because the sweep writes a
consolidated_listings.json per run — that is the ground truth for "which
sources did we actually crawl, and when".
"""
from __future__ import annotations

import collections
import datetime
import glob
import json
import os
import sqlite3

PROJ = "/workspace/hermes1/projects/aisne-property-search"
os.chdir(PROJ)

db = sqlite3.connect("listings.db")
db.row_factory = sqlite3.Row

print("=== per-source freshness (last_check is bumped on every upsert) ===")
for r in db.execute(
        "SELECT source, COUNT(*) n,"
        "       MAX(last_check)  chk,"
        "       MAX(last_seen)   seen,"
        "       MAX(first_seen)  first,"
        "       SUM(CASE WHEN first_seen >= datetime('now','-14 day') THEN 1 ELSE 0 END) new14"
        " FROM listings GROUP BY 1 ORDER BY chk DESC"):
    print(f"  {r['source']:<11} n={r['n']:<5} last_check={(r['chk'] or '')[:19]:<19} "
          f"last_seen={(r['seen'] or '')[:19]:<19} new_last_14d={r['new14']}")

print()
print("=== scan directories on disk (the ground truth: what was actually crawled) ===")
dirs = sorted(glob.glob("scans/*/"), key=os.path.getmtime, reverse=True)[:10]
if not dirs:
    print("  (none)")
for d in dirs:
    ts = datetime.datetime.fromtimestamp(os.path.getmtime(d)).strftime("%Y-%m-%d %H:%M")
    cj = os.path.join(d, "consolidated_listings.json")
    detail = ""
    if os.path.exists(cj):
        try:
            items = json.load(open(cj))
            c = collections.Counter(i.get("source") for i in items)
            detail = f"  items={len(items):<6} " + " ".join(
                f"{k}={v}" for k, v in c.most_common())
        except Exception as e:  # noqa: BLE001
            detail = f"  (unreadable: {e})"
    else:
        other = [f for f in os.listdir(d) if f.endswith(".json")]
        detail = "  (no consolidated_listings.json)" + (
            f" json={other}" if other else "")
    print(f"  {ts}  {d}{detail}")

print()
print("=== cron sweep script resolution ===")
for p in ("/opt/data/scripts/aisne_fortnightly_sweep.sh",
          "/opt/data/.hermes/scripts/aisne_fortnightly_sweep.sh",
          "/workspace/hermes1/scripts/aisne_fortnightly_sweep.sh"):
    if os.path.islink(p):
        print(f"  SYMLINK  {p} -> {os.readlink(p)}   (runner REFUSES escaping symlinks)")
    elif os.path.exists(p):
        print(f"  ok       {p}  ({os.path.getsize(p)} bytes)")
    else:
        print(f"  MISSING  {p}")
db.close()
