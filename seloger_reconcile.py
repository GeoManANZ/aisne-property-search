#!/usr/bin/env python3
"""Auto-mark SeLoger listings that have disappeared from a COMPLETE sweep.

THE HARD PART, AND WHY THIS IS NOT URL MATCHING
The same SeLoger advert is reachable at three different URLs
(/:legacyId/detail.htm, /annonce/<path>/<ref>, /wl-cdp/<code>) and which one our
parser emits can change between runs. Identity-by-URL would therefore decide that
a live listing is a different one, and mark real stock as removed. So identity is
keyed on `source_ref` — the BFF's own stable ad ref (e.g. 26R1L7BZG1JK), which we
now persist at ingest.

WHY IT REFUSES TO RUN WITHOUT coverage.complete
Absence only means "gone" if the sweep fetched EVERY advert the API reported. A
sampled pass (the 9-page fresh-stock pass, or a partial maison pass) is missing
thousands of adverts by design, so its absences are meaningless. The sweep writes
coverage.json per pass; this tool reads it and refuses on complete=False.

WHAT "GONE" MEANS HERE — NOT "SOLD"
Absence proves the advert was delisted: withdrawn, under offer, or sold. It does
NOT prove a sale, so these rows are recorded as `withdrawn`, never `sold`. Only a
detail page saying "Vendu" (check_liveness.py) justifies 'sold'.

TWO-STRIKE RULE
A listing is marked gone only after appearing absent from two consecutive
complete sweeps. One missing sweep is noise — portals reshuffle sort order and a
transient API hiccup loses a page.

Usage:
    seloger_reconcile.py                 # dry run, prints what it would do
    seloger_reconcile.py --apply         # write miss_count / gone
    seloger_reconcile.py --dir seloger_pages_House   # single pass dir
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
# Strikes may only be applied when EVERY pass in the required set is complete. A
# complete Building (immeuble) pass says NOTHING about maisons, so treating its
# absences as evidence would strike every house in the database. Absence is only
# evidence against the estate type that pass actually enumerated.
REQUIRED_DIRS = ["seloger_pages_Building", "seloger_pages_House"]
LEGACY_DIRS = ["seloger_pages"]
MAX_AGE_HOURS = 36       # a stale pass describes a market that has moved on
STRIKES_TO_GONE = 2


def load_pass(d: Path, max_age_hours: float) -> tuple[set[str], str, list[str]]:
    """Return (live_refs, note, problems) for one pass directory."""
    cov_f = d / "coverage.json"
    json_f = d / "all_listings_api.json"
    problems: list[str] = []
    if not cov_f.exists() or not json_f.exists():
        # NOTE must stay empty here: the caller counts non-empty notes as
        # "complete pass available". Returning a message in the note slot made a
        # missing pass count as present and the refusal gate never fired.
        return set(), "", [f"{d.name}: no coverage.json/all_listings_api.json"]
    cov = json.loads(cov_f.read_text())
    age_h = (datetime.now(timezone.utc)
             - datetime.fromisoformat(cov["at"])).total_seconds() / 3600
    if not cov.get("complete"):
        problems.append(f"{d.name}: coverage NOT complete "
                        f"({cov.get('fetched')}/{cov.get('total_reported')}) — "
                        f"absences are meaningless, skipped")
        return set(), "", problems
    if age_h > max_age_hours:
        problems.append(f"{d.name}: pass is {age_h:.1f}h old (> {max_age_hours}h) — skipped")
        return set(), "", problems
    items = json.loads(json_f.read_text())
    refs = {i["ref"] for i in items if i.get("ref")}
    note = (f"{d.name}: complete pass, {len(refs)} live refs, {age_h:.1f}h old")
    return refs, note, problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(HERE / "listings.db"))
    ap.add_argument("--dir", action="append", default=None,
                    help="pass directory (repeatable); default: all known")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--max-age-hours", type=float, default=MAX_AGE_HOURS)
    args = ap.parse_args()

    dirs = [Path(p) for p in (args.dir or REQUIRED_DIRS)]
    dirs = [p if p.is_absolute() else HERE / p for p in dirs]

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    # self-heal the columns this tool depends on
    cols = [c[1] for c in con.execute("PRAGMA table_info(listings)")]
    for name, decl in (("source_ref", "TEXT"), ("miss_count", "INTEGER DEFAULT 0")):
        if name not in cols:
            con.execute(f"ALTER TABLE listings ADD COLUMN {name} {decl}")
    con.commit()

    live: set[str] = set()
    notes, problems = [], []
    for d in dirs:
        refs, note, probs = load_pass(d, args.max_age_hours)
        live |= refs
        if note:
            notes.append(note)
        problems += probs
    for p in problems:
        print(f"  ! {p}")

    expected = len(dirs)
    if len(notes) < expected:
        print(f"REFUSED: only {len(notes)}/{expected} required passes are complete and "
              f"fresh. Striking listings from a partially-covered sweep would mark live "
              f"stock as removed. Nothing written.")
        return 2

    print("\n".join(f"  {n}" for n in notes))

    rows = con.execute("""SELECT url, source_ref, miss_count, title, price_eur,
                                 surface_m2, location
                          FROM listings
                          WHERE source='seloger' AND status='active'""").fetchall()
    known = [r for r in rows if r["source_ref"]]
    no_ref = [r for r in rows if not r["source_ref"]]
    absent = [r for r in known if r["source_ref"] not in live]
    seen = [r for r in known if r["source_ref"] in live]

    print(f"\nSeLoger active rows : {len(rows)}")
    print(f"  identifiable      : {len(known)}  (source_ref present)")
    print(f"  no source_ref     : {len(no_ref)}  (never marked — cannot identify safely)")
    print(f"  seen in sweep     : {len(seen)}  -> miss_count reset to 0")
    print(f"  ABSENT from sweep : {len(absent)}")

    to_gone, to_strike = [], []
    for r in absent:
        strikes = (r["miss_count"] or 0) + 1
        (to_gone if strikes >= STRIKES_TO_GONE else to_strike).append((r, strikes))

    print(f"    -> reaches {STRIKES_TO_GONE} strikes, mark gone : {len(to_gone)}")
    print(f"    -> first strike, still live   : {len(to_strike)}")
    for r, s in to_gone[:10]:
        print(f"       GONE  {r['location'] or '?':28s} €{r['price_eur'] or 0:>9,} "
              f"{r['surface_m2'] or 0:>6.0f}m²  ref={r['source_ref']}")
    if len(to_gone) > 10:
        print(f"       ... +{len(to_gone)-10} more")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.executemany("UPDATE listings SET miss_count=0 WHERE url=?",
                    [(r["url"],) for r in seen])
    con.executemany("UPDATE listings SET miss_count=? WHERE url=?",
                    [(s, r["url"]) for r, s in to_strike])
    for r, _s in to_gone:
        con.execute("""UPDATE listings SET status='gone', gone_at=?, gone_reason='withdrawn'
                       WHERE url=?""", (now, r["url"]))
        con.execute("""UPDATE listings SET miss_count=? WHERE url=?""",
                    (STRIKES_TO_GONE, r["url"]))
        # disposals: insert only columns that exist (keeps this tool self-sufficient
        # when the schema drifts) — never claim 'sold' from absence alone.
        dcols = [c[1] for c in con.execute("PRAGMA table_info(disposals)")]
        payload = {
            "url": r["url"], "source": "seloger", "town": r["location"],
            "price_eur": r["price_eur"], "surface_m2": r["surface_m2"],
            "gone_at": now, "gone_reason": "withdrawn",
            "marker": f"absent from {STRIKES_TO_GONE} consecutive complete BFF sweeps",
            "eur_m2": (round(r["price_eur"] / r["surface_m2"])
                       if r["price_eur"] and r["surface_m2"] else None),
        }
        use = [k for k in payload if k in dcols]
        con.execute(f"INSERT INTO disposals ({','.join(use)}) "
                    f"VALUES ({','.join('?'*len(use))})", [payload[k] for k in use])
        if "listing_events" in [t[0] for t in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]:
            ecols = {c[1] for c in con.execute("PRAGMA table_info(listing_events)")}
            ev = {"url": r["url"], "event": "sweep_miss_gone", "at": now,
                  "detail": f"absent from complete BFF sweep x{STRIKES_TO_GONE}"}
            use = [k for k in ev if k in ecols]
            con.execute(f"INSERT INTO listing_events ({','.join(use)}) "
                        f"VALUES ({','.join('?'*len(use))})", [ev[k] for k in use])
    con.commit()
    print(f"\nAPPLIED: {len(seen)} reset, {len(to_strike)} first-strike, "
          f"{len(to_gone)} marked gone (reason=withdrawn, NOT sold)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
