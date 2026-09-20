#!/usr/bin/env python3
"""Categorised, context-rich briefs — the answer to "just numbers and €/m²".

For each candidate: which lane it belongs to and WHY (the matched evidence, quoted
from the advert itself), how long it has been on the market, whether the vendor has
already cut the price, and how it sits against its own peer group.

Benchmarks are computed from OUR OWN criteria set (same lane, same surface band), so
every figure is reproducible from listings.db and nothing is estimated or invented.
No rent estimate is shown: we do not scrape rents yet, and a fabricated yield is
worse than no yield.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

import categorize
try:
    from ingest_rents import load_benchmarks as _load_rent_bench, town_key as _town_key
except Exception:                      # noqa: BLE001 - rents are optional enrichment
    _load_rent_bench = _town_key = None

HERE = Path(__file__).parent

WHAT_IT_IS = {
    "yield": "Income asset — value is the rent roll, not the finish (multi-unit / let-able / commercial income).",
    "renovation": "Works project — the price reflects the condition; budget acquisition + works + time.",
    "livein": "Habitable home — move in or light cosmetic work.",
    "commercial": "Non-housing — warehouse/commercial premises. Different buyer pool, different financing, own exit risk.",
    "unclassified": "No condition evidence in the advert — treat as unknown, verify by visit.",
}


def conn(db: str) -> sqlite3.Connection:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(HERE / "listings.db"))
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--lane", default=None, help="yield|renovation|livein|commercial")
    ap.add_argument("--no-header", action="store_true",
                    help="suppress the summary header (used when concatenating per-lane runs)")
    args = ap.parse_args()

    con = conn(args.db)
    rows = con.execute("""
        SELECT url, source, title, description, price_eur, surface_m2, dpe_energy,
               location, first_seen, category, category_flags, category_confidence, agency
        FROM listings
        WHERE status='active' AND status!='invalid'
          AND surface_m2 BETWEEN 150 AND 1500
          AND price_eur BETWEEN 10000 AND 220000
    """).fetchall()

    items = []
    for r in rows:
        res = categorize.classify(r["title"], r["description"], r["dpe_energy"], r["surface_m2"])
        cat = r["category"] or res["category"]
        if args.lane and cat != args.lane:
            continue
        ev = []
        for lane, words in (res.get("why") or {}).items():
            if lane == cat:
                ev = words
        items.append({**dict(r), "cat": cat, "why": ev, "flags": (r["category_flags"] or ""),
                      "pm2": r["price_eur"] / r["surface_m2"] if r["surface_m2"] else None})

    # peer benchmark: median €/m² within the same lane (rung of the same market)
    bench: dict[str, float] = {}
    for cat in {i["cat"] for i in items}:
        vals = [i["pm2"] for i in items if i["cat"] == cat and i["pm2"]]
        if len(vals) >= 3:
            bench[cat] = statistics.median(vals)
    allvals = [i["pm2"] for i in items if i["pm2"]]
    bench["_all"] = statistics.median(allvals) if allvals else 0

    items.sort(key=lambda i: i["pm2"] or 9e9)

    # Cross-source dedup: one property is listed on up to four portals, so without
    # this the "top 10" is the same building repeated (observed: a Liesse immeuble
    # occupied 4 of the top 6 slots). Same fingerprint as recommendations_report.py:
    # price + surface + normalised town.
    import re
    merged: dict[tuple, dict] = {}
    for i in items:
        town = re.sub(r"[^a-z]", "", re.split(r"[\d(]", (i["location"] or "").lower())[0])
        key = (round(i["price_eur"] or 0, -2), round(i["surface_m2"] or 0, -1), town)
        if key not in merged:
            i["also_on"] = []
            merged[key] = i
        else:
            keep = merged[key]
            keep["also_on"].append(i["source"])
            # prefer the record carrying the most advert evidence
            if len(i["why"]) > len(keep["why"]) or \
                    (i["category_confidence"] == "high" and keep["category_confidence"] != "high"):
                i["also_on"] = keep["also_on"] + [keep["source"]]
                merged[key] = i
    items = sorted(merged.values(), key=lambda i: i["pm2"] or 9e9)

    now = datetime.now(timezone.utc)

    # Rent benchmarks -> gross yield. Which band applies depends on the implied
    # letting strategy: a multi-unit immeuble is let as FLATS (mid band), while a
    # single large dwelling is let as one house (large band). Using one commune-wide
    # €/m² rate for both would badly overstate the house.
    rentbench = {}
    if _load_rent_bench:
        try:
            rentbench = _load_rent_bench(con)
        except Exception:              # noqa: BLE001
            rentbench = {}

    lanes: dict[str, int] = {}
    for i in items:
        lanes[i["cat"]] = lanes.get(i["cat"], 0) + 1
    if not args.no_header:
        print(f"# Categorised briefs — top {args.top} by €/m² ({len(items)} candidates)")
        print(f"**Date:** {now:%Y-%m-%d %H:%M} UTC · lanes: "
              + " · ".join(f"{k} {v}" for k, v in sorted(lanes.items(), key=lambda x: -x[1])))
        print(f"**Peer medians (this criteria set):** "
              + " · ".join(f"{k} €{v:,.0f}/m²" for k, v in bench.items() if k != "_all"))
        print()

    for n, i in enumerate(items[:args.top], 1):
        b = bench.get(i["cat"], bench["_all"])
        delta = f"{(i['pm2']/b - 1) * 100:+.0f}% vs {i['cat']} peer median" if b else ""
        try:
            dom = (now - datetime.fromisoformat(str(i["first_seen"]).replace("Z", "+00:00"))).days
        except (ValueError, TypeError):
            dom = None
        drops = con.execute("""SELECT COUNT(*) c FROM price_history
                               WHERE url=? AND price_eur < ?""",
                            (i["url"], i["price_eur"])).fetchone()["c"]
        print(f"**{n}. [{i['cat'].upper()}·{i['category_confidence'] or '?'}] "
              f"{i['location'] or '?'} — €{i['price_eur']:,} · {i['surface_m2']:,.0f} m² · "
              f"€{i['pm2']:,.0f}/m²**")
        print(f"   {WHAT_IT_IS.get(i['cat'], '')}")
        if i["why"]:
            print(f"   advert says: {'; '.join(repr(w) for w in i['why'])}")
        if i["flags"]:
            print(f"   also: {i['flags']} (secondary lane)")
        facts = [f"DPE {i['dpe_energy'] or 'not stated'}"]
        if dom is not None:
            facts.append(f"on market {dom}d (leverage)" if dom > 45 else f"on market {dom}d")
        if drops:
            facts.append(f"{drops} price cut(s) recorded")
        if delta:
            facts.append(delta)
        if i.get("also_on"):
            facts.append("also listed on " + ", ".join(sorted(set(i["also_on"]))))
        print(f"   {' · '.join(facts)}")

        # yield, only where a real rent benchmark exists
        if rentbench and i["cat"] in ("yield", "commercial", "renovation", "livein"):
            tk = _town_key(i["location"])
            bands = rentbench.get(tk) or rentbench.get("_all") or {}
            basis = "commune" if rentbench.get(tk) else "dept-wide (no commune rate)"
            want = ["mid", "small"] if i["cat"] == "yield" else ["large", "mid"]
            pick = next(((b, bands[b]) for b in want if b in bands), None)
            if pick and i["surface_m2"]:
                band, (rate, nobs) = pick
                rent = rate * i["surface_m2"]
                gross = 12 * rent / i["price_eur"] * 100
                print(f"   rent model: €{rent:,.0f}/month (surface x €{rate:.2f}/m²/mo, "
                      f"{band} band, n={nobs}, {basis})")
                print(f"   gross yield: {gross:.1f}%  → net after taxe foncière, charges, "
                      f"vacancy & management ≈ {gross*0.65:.1f}-{gross*0.75:.1f}% "
                      f"(typical drag, not measured)")
            else:
                print("   rent model: no rent benchmark for this area — yield not estimated")
        print(f"   {i['url']}")
        print()

    print("Benchmarks are medians of this same criteria set — not asking prices of "
          "unrelated stock, and not estimates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
