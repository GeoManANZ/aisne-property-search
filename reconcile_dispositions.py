#!/usr/bin/env python3
"""Repair pass: reconcile dispositions and surfaces after a bulk ingest.

Two data-integrity repairs that a bulk re-scrape can reintroduce, so this is
re-runnable (idempotent) and safe to call after any import.

1. DISPOSITION REVERSAL
   `upsert_listing` re-activates a URL that reappears in a search feed. Before
   the fix, that left the row contradicting itself: status='active' AND
   gone_at set, plus a `disposals` record that still counted as a sale. Found
   2026-09-16: 5 rows re-activated by a full sweep, one a false "sold" at
   EUR45,000 for a property that is back on the market.
   Now: clear gone_at/gone_reason, mark the disposal reversed, log the event.
   Reversed disposals stay in the table for audit but are excluded from stats,
   so a false sale can never inflate "what sold".

2. SURFACE vs TITLE
   ParuVendu cards carry the LAND area in the size field while the title states
   the real building surface ("Maison - 3 pièce(s) - 90 m²" stored as 1,635 m²).
   That ranked a 90 m² house at EUR30/m² above genuine 150 m²+ stock, and let it
   pass a >=150 m² criteria it should fail. Re-validates stored rows with the
   same scoped rule the ingest uses (pièce marker + >1.5x discrepancy).
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from listings_db import validate_listing  # noqa: E402

DB = Path(__file__).resolve().parent / "listings.db"
now = datetime.now(timezone.utc).isoformat(timespec="seconds")

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

# Make the tool self-sufficient: it may run before check_liveness has migrated.
dcols = {r[1] for r in con.execute("PRAGMA table_info(disposals)")}
for col in ("reversed_at", "reversed_reason"):
    if col not in dcols:
        con.execute(f"ALTER TABLE disposals ADD COLUMN {col} TEXT")
        print(f"migration: added disposals.{col}")

# ---- 1. reverse stale dispositions on re-activated rows --------------------
rows = con.execute(
    "SELECT url, source, gone_at, gone_reason FROM listings"
    " WHERE status='active' AND gone_at IS NOT NULL").fetchall()
for r in rows:
    con.execute("UPDATE listings SET gone_at=NULL, gone_reason=NULL WHERE url=?",
                (r["url"],))
    con.execute("UPDATE disposals SET reversed_at=?, reversed_reason=?"
                " WHERE url=? AND reversed_at IS NULL",
                (now, "reappeared in search feed (repair pass)", r["url"]))
    con.execute("INSERT INTO listing_events (url, source, observed_at, event, note)"
                " VALUES (?,?,?,?,?)",
                (r["url"], r["source"], now, "restored",
                 f"repair pass: stale gone_at cleared (was '{r['gone_reason']}')"))
print(f"[1] cleared stale gone_at on {len(rows)} re-activated row(s), "
      f"reversed their disposal records")

# ---- 2. surface vs title ---------------------------------------------------
fixed = 0
for r in con.execute(
        "SELECT url, source, title, surface_m2 FROM listings"
        " WHERE surface_m2 IS NOT NULL AND title LIKE '%pi%ce%'"):
    cleaned, errors = validate_listing({
        "url": r["url"], "source": r["source"], "title": r["title"],
        "surface_m2": r["surface_m2"]})
    new_s = cleaned.get("surface_m2")
    if new_s and new_s != r["surface_m2"]:
        con.execute("UPDATE listings SET surface_m2=? WHERE url=?", (new_s, r["url"]))
        con.execute("INSERT INTO listing_events (url, source, observed_at, event, note)"
                    " VALUES (?,?,?,?,?)",
                    (r["url"], r["source"], now, "surface_corrected",
                     f"{r['surface_m2']:.0f} -> {new_s:.0f} m² (size field held the land area)"))
        fixed += 1
        print(f"    {r['source']:<11} {r['surface_m2']:>7.0f} -> {new_s:>6.0f} m²  "
              f"{(r['title'] or '')[:44]}")
print(f"[2] corrected {fixed} surface(s) that were land areas")

# ---- 3. re-audit every surface correction against the CURRENT rule --------
# A rule change must be re-applied to rows it already touched, or the DB keeps
# the damage from the old version. The event log stores "OLD -> NEW m²", so the
# pre-repair value is recoverable: re-test the title with today's rule; if it no
# longer matches, restore the original figure.
print()
print("[3] re-auditing previous surface corrections against the current rule")
reverted = confirmed = 0
for ev in con.execute(
        "SELECT url, source, note FROM listing_events"
        " WHERE event='surface_corrected' ORDER BY id").fetchall():
    old = new = None
    mm = re.search(r"(\d+)\s*->\s*(\d+)\s*m", ev["note"] or "")
    if mm:
        old, new = float(mm.group(1)), float(mm.group(2))
    if old is None:
        continue
    row = con.execute("SELECT title, surface_m2 FROM listings WHERE url=?",
                      (ev["url"],)).fetchone()
    if not row:
        continue
    # Re-test against the ORIGINAL value, not the current one: a row that was
    # already corrected no longer looks discrepant, so testing the current value
    # would silently "confirm" a wrong correction.
    cleaned, errs = validate_listing({"url": ev["url"], "source": ev["source"],
                                      "title": row["title"], "surface_m2": old})
    fires = any("looks like the LAND area" in e for e in errs)
    want = cleaned["surface_m2"] if fires else old
    if want != row["surface_m2"]:
        con.execute("UPDATE listings SET surface_m2=? WHERE url=?", (want, ev["url"]))
        if not fires:
            reverted += 1
            print(f"    REVERT   {row['surface_m2']:.0f} -> {want:.0f} m²  "
                  f"(current rule does not match) {(row['title'] or '')[:38]}")
        else:
            print(f"    adjust   {row['surface_m2']:.0f} -> {want:.0f} m²  "
                  f"{(row['title'] or '')[:38]}")
    else:
        confirmed += 1
print(f"    {reverted} reverted to the original value, {confirmed} corrections confirmed")

con.commit()

print()
print("=== integrity now ===")
c = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
checks = {
    "active rows carrying gone_at":
        c("SELECT COUNT(*) FROM listings WHERE status='active' AND gone_at IS NOT NULL"),
    "active rows still counted as disposals":
        c("SELECT COUNT(*) FROM disposals d JOIN listings l USING(url)"
          " WHERE l.status='active' AND d.reversed_at IS NULL"),
    "live (non-reversed) disposals":
        c("SELECT COUNT(*) FROM disposals WHERE reversed_at IS NULL"),
    "reversed disposals (audit trail)":
        c("SELECT COUNT(*) FROM disposals WHERE reversed_at IS NOT NULL"),
}
for k, v in checks.items():
    print(f"  {k:<42} {v}")
con.close()
