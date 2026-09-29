#!/usr/bin/env python3
"""Gross yield per listing, using the MTE/ANIL 'Carte des loyers' benchmark.

The SeLoger rent axis is DataDome-blocked on every Webshare French IP (verified
2026-09-29, 3/3 sessions), so instead of scraping asking rents we use the
Ministry of Housing's commune-level indicators built from ~9M rental ads
(leboncoin + SeLoger + PAP). Same market, already aggregated, free and unblockable.

Every figure carries its trust level:
  ✓ commune  = modelled on THIS commune's own ads (nbobs_com >= 30, R2 >= 0.5)
  ⚠ mesh     = predicted from a wider mesh — indicative only, never for a purchase
  ⚠ thin     = <30 observations in the commune

Attribution (licence requirement):
  "Estimations ANIL, à partir des données du Groupe SeLoger et de leboncoin"
"""
from __future__ import annotations

import re
import sqlite3
import sys
import unicodedata

DB = "listings.db"


def norm(s: str) -> str:
    """Accent-strip + drop postcode/dept so 'Guise (02140)' matches 'Guise'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(?\b\d{2,5}\b\)?", " ", s)          # postcode / dept
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def kind_for(title: str) -> str:
    t = (title or "").lower()
    if re.search(r"\bimmeuble\b|\bappartements?\b|\bmaison de rapport\b", t):
        return "app3"          # multi-unit / lettable stock
    if re.search(r"\bmaison\b|\blong[eè]re\b|\bferme\b|\bcorps de ferme\b", t):
        return "maison"
    return "app"


def main() -> int:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    bench = {}
    for r in con.execute("SELECT kind, commune, loy_m2, loy_lo, loy_hi, typpred,"
                         " nbobs_com, r2_adj FROM rent_benchmarks WHERE dept='02'"):
        bench[(r["kind"], norm(r["commune"]))] = r

    rows = con.execute(
        "SELECT title, location, price_eur, surface_m2, url, source FROM listings "
        "WHERE status='active' AND surface_m2 BETWEEN 150 AND 1500 "
        "AND price_eur BETWEEN 10000 AND 220000").fetchall()

    out, unmatched, dedup = [], 0, {}
    for r in rows:
        k = kind_for(r["title"])
        b = bench.get((k, norm(r["location"])))
        if not b or not b["loy_m2"] or not r["price_eur"] or not r["surface_m2"]:
            unmatched += 1
            continue
        # Cross-source dedupe: one building shows on up to four portals, so an
        # undeduped yield list repeats the same asset (Liesse appeared 4x).
        fp = (int(r["price_eur"]), int(r["surface_m2"]), norm(r["location"]))
        if fp in dedup:
            dedup[fp]["alts"].append(r["source"])
            continue
        # Plausibility: the benchmark is a DWELLING rent/m². Multiplying it by a
        # 1,354 m² derelict building's full surface yields fantasy 100%+ returns.
        # Model rent only where the whole surface is plausibly lettable; for the
        # rest report the benchmark and refuse the arithmetic.
        modelled = r["surface_m2"] <= 250
        monthly = b["loy_m2"] * r["surface_m2"] if modelled else None
        gy = (monthly * 12.0 / r["price_eur"] * 100.0) if monthly else None
        trusted = (b["typpred"] == "commune" and b["nbobs_com"] >= 30
                   and (b["r2_adj"] or 0) >= 0.5)
        dedup[fp] = {"gy": gy, "row": r, "b": b, "monthly": monthly,
                     "trusted": trusted, "alts": []}
    out = sorted(dedup.values(), key=lambda x: -(x["gy"] or -1))
    print(f"gross yield on {len(out)} deduped criteria listings "
          f"({unmatched} unmatched to a benchmark)\n")
    print("=== TOP 10 by estimated gross yield (surface <=250 m² only) ===")
    for e in out[:10]:
        gy, r, b, monthly = e["gy"], e["row"], e["b"], e["monthly"]
        if gy is None:
            continue
        flag = "✓ commune" if e["trusted"] else "⚠ " + (
            "thin" if b["nbobs_com"] < 30 else "mesh")
        alts = f"  +{len(e['alts'])} other portals" if e["alts"] else ""
        print(f"  {gy:5.1f}%  €{r['price_eur']:,} · {r['surface_m2']:.0f} m² · "
              f"{(r['location'] or '')[:18]:18s} · €{b['loy_m2']:.2f}/m² → "
              f"€{monthly:,.0f}/mo · {flag}{alts}")
        print(f"          {r['title'][:44]}")
        print(f"          {r['url']}")

    n_unmodelled = sum(1 for e in out if e["gy"] is None)
    print(f"\n=== not modelled on purpose ({n_unmodelled}) ===")
    print("  surface >250 m²: the benchmark is a DWELLING rent/m², so a whole-building")
    print("  rent cannot be derived without the unit split. Benchmark shown instead:")
    for e in [x for x in out if x["gy"] is None][:5]:
        r, b = e["row"], e["b"]
        print(f"  €{r['price_eur']:,} · {r['surface_m2']:.0f} m² · "
              f"{(r['location'] or '')[:18]:18s} · benchmark €{b['loy_m2']:.2f}/m²")
        print(f"          {r['url']}")

    trusted_only = [e for e in out if e["trusted"] and e["gy"] is not None]
    if trusted_only:
        print(f"\n=== trustworthy AND modelled ({len(trusted_only)}) ===")
        for e in trusted_only[:6]:
            r = e["row"]
            print(f"  {e['gy']:5.1f}%  €{r['price_eur']:,} · {r['surface_m2']:.0f} m² · "
                  f"{(r['location'] or '')[:18]:18s} · {r['url']}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
