#!/usr/bin/env python3
"""Ingest SeLoger RENT passes into a `rents` table and derive rent benchmarks.

Why: we scrape only `distributionTypes:["Buy"]`, so we can say a building is cheap
and nothing about what it earns. Rents convert the yield lane from "€675/m²" into
"€X/month, Y% gross yield".

THREE GUARDS, because a wrong rent silently fabricates a yield:
  1. Rents never touch the `listings` table (enforced in seloger_api_sweep.py, and
     again here by writing only to `rents`).
  2. Sanity band: Aisne monthly rents run ~€300-€2,500. Anything outside €150-€6,000
     is a parse artefact (a price-per-m², an annual figure, a sale price leaking in)
     and is rejected, with the count printed.
  3. Benchmarks need >=3 observations. Below that a commune gets no rate rather than
     a rate built from one advert.

Bands: rents per m² fall as units get bigger, so a single commune-wide rate would
overstate a large house badly. Benchmarks are therefore computed per size band and
the caller picks the band matching the implied letting strategy (multi-unit -> flat
rates; single large dwelling -> large-unit rate).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
RENT_MIN, RENT_MAX = 150.0, 6000.0     # sanity band for a monthly rent in dept 02
BENCH_MIN_OBS = 3

BANDS = (("small", 0, 60), ("mid", 60, 120), ("large", 120, 10_000))


def town_key(location: str | None) -> str:
    """Normalise a portal location string to a commune key.

    Portals write the same commune as 'Liesse-Notre-Dame 02350',
    'Liesse-Notre-Dame (02350)' and 'Liesse-Notre-Dame (02)' — all must key alike
    or the benchmark never matches the sale listing it is pricing.
    """
    if not location:
        return ""
    head = re.split(r"[\d(]", location.lower())[0]
    return re.sub(r"[^a-z]", "", head)


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS rents (
            url              TEXT PRIMARY KEY,
            source_ref       TEXT,
            rent_eur         REAL,
            surface_m2       REAL,
            rent_per_m2      REAL,
            town             TEXT,
            town_key         TEXT,
            estate_type      TEXT,
            dpe_energy       TEXT,
            title            TEXT,
            description      TEXT,
            first_seen       TEXT,
            last_seen        TEXT
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_rents_town ON rents(town_key)")
    con.commit()


def band_of(surface: float | None) -> str | None:
    if not surface:
        return None
    for name, lo, hi in BANDS:
        if lo <= surface < hi:
            return name
    return None


def load_benchmarks(con: sqlite3.Connection) -> dict:
    """{town_key: {band: (median_eur_m2_month, n)}} plus a '_all' fallback."""
    out: dict[str, dict] = {}
    for r in con.execute("""SELECT town_key, surface_m2, rent_eur FROM rents
                            WHERE rent_eur IS NOT NULL AND surface_m2 >= 15"""):
        b = band_of(r["surface_m2"])
        if not b:
            continue
        rate = r["rent_eur"] / r["surface_m2"]
        if not (0.5 <= rate <= 30):      # €/m²/month sanity
            continue
        out.setdefault(r["town_key"] or "", {}).setdefault(b, []).append(rate)
        out.setdefault("_all", {}).setdefault(b, []).append(rate)
    return {t: {b: (statistics.median(v), len(v)) for b, v in bands.items()
                if len(v) >= BENCH_MIN_OBS}
            for t, bands in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(HERE / "listings.db"))
    ap.add_argument("--dir", action="append", default=None,
                    help="pass dir(s) containing all_listings_api.json")
    ap.add_argument("--benchmarks", action="store_true", help="print derived rates")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    ensure_schema(con)

    dirs = args.dir or sorted(p.name for p in HERE.glob("seloger_pages_*Rent*"))
    dirs = [d if str(d).startswith("/") else str(HERE / d) for d in dirs]

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_in, total_seen, rejected, dupes = 0, 0, 0, 0
    for d in dirs:
        f = Path(d) / "all_listings_api.json"
        if not f.exists():
            print(f"  ! {d}: no all_listings_api.json")
            continue
        est = "Rent/" + (Path(d).name.split("_")[-1].replace("Rent", "") or "?")
        items = json.loads(f.read_text())
        print(f"  {Path(d).name}: {len(items)} raw ({est})")
        for it in items:
            total_in += 1
            rent = it.get("price_eur")
            surf = it.get("surface_m2")
            if rent is None or not (RENT_MIN <= rent <= RENT_MAX):
                rejected += 1
                continue
            tk = town_key(it.get("location"))
            row = (it.get("url"), it.get("ref"), rent, surf,
                   round(rent / surf, 2) if surf else None,
                   it.get("location"), tk, est, it.get("dpe_energy"),
                   it.get("title"), it.get("description"), now, now)
            try:
                con.execute("""INSERT INTO rents (url,source_ref,rent_eur,surface_m2,
                               rent_per_m2,town,town_key,estate_type,dpe_energy,title,
                               description,first_seen,last_seen)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(url) DO UPDATE SET
                                 rent_eur=excluded.rent_eur,
                                 surface_m2=excluded.surface_m2,
                                 rent_per_m2=excluded.rent_per_m2,
                                 last_seen=excluded.last_seen""", row)
                total_seen += 1
            except sqlite3.Error as e:
                print(f"     ! insert failed: {str(e)[:60]}")
        con.commit()

    n = con.execute("SELECT COUNT(*) c FROM rents").fetchone()["c"]
    print(f"\ningest: {total_in} read, {total_seen} written, {rejected} rejected "
          f"outside €{RENT_MIN:.0f}-€{RENT_MAX:.0f}/month, {dupes} dupes")
    print(f"rents table: {n} rows")
    for r in con.execute("""SELECT estate_type, COUNT(*) c,
                                   ROUND(AVG(rent_eur)) avg_rent
                            FROM rents GROUP BY 1 ORDER BY 2 DESC"""):
        print(f"  {r['estate_type']:14s} {r['c']:5d} rents, avg €{r['avg_rent']:,.0f}/month")

    if args.benchmarks or n:
        bench = load_benchmarks(con)
        print(f"\n=== rate benchmarks (€/m²/month, >= {BENCH_MIN_OBS} obs per band) ===")
        for band in ("small", "mid", "large"):
            if band in bench.get("_all", {}):
                med, cnt = bench["_all"][band]
                print(f"  dept-wide {band:5s} {med:5.2f} €/m²/month  (n={cnt})")
        print(f"\n  communes with a usable rate: {len([k for k in bench if k != '_all'])}")
        rows = sorted(((k, v) for k, v in bench.items() if k != "_all"),
                      key=lambda kv: -(kv[1].get("mid", (0, 0))[1]))
        for k, bands in rows[:18]:
            desc = "  ".join(f"{b}={bands[b][0]:.2f}({bands[b][1]})" for b in ("small", "mid", "large")
                             if b in bands)
            print(f"    {k:26s} {desc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
