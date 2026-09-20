#!/usr/bin/env python3
"""Per-cycle summary: what's NEW, what DROPPED in price, what SOLD.

Printed to STDOUT because cron delivers stdout verbatim (stderr is invisible) —
this is the block that makes the fortnightly report carry the three questions the
user actually asks: new listings, price updates, and what left the market.

Three data sources, kept distinct on purpose:
  * first_seen  -> genuinely NEW stock (a re-scrape bumps last_seen, not this)
  * price_history -> price changes, corroborated (newest < previous)
  * disposals   -> left the market, with the evidence string and €/m² at exit.
                   Reversed rows are excluded: a disposal that was later
                   contradicted by re-appearance is NOT a sale.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent / "listings.db"
MIN_SURF, MAX_SURF = 150, 1500
MIN_PRICE, MAX_PRICE = 10_000, 220_000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()
    d = args.days

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    print(f"=== NEW LISTINGS (first_seen within {d} days, in criteria) ===")
    n_new = con.execute(
        f"SELECT COUNT(*) FROM listings WHERE first_seen >= datetime('now','-{d} day')"
        f" AND status='active' AND surface_m2 BETWEEN {MIN_SURF} AND {MAX_SURF}"
        f" AND price_eur BETWEEN {MIN_PRICE} AND {MAX_PRICE}").fetchone()[0]
    print(f"  {n_new} new criteria-matching listing(s)")
    for r in con.execute(
            f"SELECT source, COUNT(*) n FROM listings"
            f" WHERE first_seen >= datetime('now','-{d} day')"
            f" AND status='active' GROUP BY 1 ORDER BY 2 DESC"):
        print(f"    {r['source']:<11} {r['n']}")

    print()
    print(f"=== PRICE DROPS (corroborated, last {d} days, in criteria) ===")
    drops = con.execute(f"""
        SELECT h.url, l.source, l.location, l.surface_m2,
               (SELECT price_eur FROM price_history x
                 WHERE x.url=h.url AND x.id<h.id ORDER BY x.id DESC LIMIT 1) old_p,
               h.price_eur new_p
        FROM price_history h
        JOIN listings l ON l.url = h.url
        WHERE h.seen_at >= datetime('now','-{d} day')
          AND l.status='active'
          AND l.surface_m2 BETWEEN {MIN_SURF} AND {MAX_SURF}""").fetchall()
    real = []
    for r in drops:
        if r["old_p"] and r["new_p"] and r["new_p"] < r["old_p"]:
            real.append((100 * (r["old_p"] - r["new_p"]) / r["old_p"], r))
    real.sort(key=lambda x: -x[0])
    print(f"  {len(real)} genuine drop(s)")
    for pct, r in real[:args.top]:
        print(f"    -{pct:5.1f}%  {(r['location'] or '')[:22]:<22} "
              f"€{r['old_p']:>7,.0f} -> €{r['new_p']:>7,.0f}  {r['surface_m2']:.0f}m²  "
              f"({r['source']})")

    print()
    print(f"=== WHAT SOLD / LEFT THE MARKET (last {d} days) ===")
    for r in con.execute(f"""
            SELECT gone_reason, COUNT(*) n, ROUND(AVG(price_eur)) ap,
                   ROUND(AVG(eur_m2)) am
            FROM disposals WHERE reversed_at IS NULL
              AND gone_at >= datetime('now','-{d} day') GROUP BY 1 ORDER BY 2 DESC"""):
        print(f"  {r['gone_reason']:<11} {r['n']:<4} avg €{r['ap'] or 0:,.0f} "
              f"({r['am'] or 0:.0f} €/m²)")
    print("  --- individual exits (price / m² at exit / evidence) ---")
    for r in con.execute(f"""
            SELECT gone_at, gone_reason, source, town, price_eur, surface_m2, eur_m2, marker
            FROM disposals WHERE reversed_at IS NULL
              AND gone_at >= datetime('now','-{d} day')
            ORDER BY gone_reason, gone_at DESC LIMIT {args.top}"""):
        print(f"    {(r['gone_at'] or '')[:10]} {r['gone_reason']:<10} "
              f"{(r['town'] or '')[:20]:<20} €{r['price_eur'] or 0:>7,.0f} "
              f"{r['surface_m2'] or 0:>5.0f}m² €{r['eur_m2'] or 0:>5,.0f}/m²  "
              f"{(r['marker'] or '')[:34]}")
    tot = con.execute("SELECT COUNT(*) FROM disposals WHERE reversed_at IS NULL").fetchone()[0]
    rev = con.execute("SELECT COUNT(*) FROM disposals WHERE reversed_at IS NOT NULL").fetchone()[0]
    print(f"  lifetime: {tot} disposals recorded, {rev} reversed (excluded as false sales)")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
