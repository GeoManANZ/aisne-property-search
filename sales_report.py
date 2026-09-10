#!/usr/bin/env python3
"""Interrogate the sale/sold database: what sold, where, and at what price.

Two independent sources, kept separate on purpose:
  * dvf_sales_clean — OFFICIAL notarial sales (Etalab DVF). Achieved prices.
  * listings / disposals — OUR tracked portal listings (+ when they left market).

Canned reports (no args = overview):
  --overview                headline counts, both sources
  --communes N              top communes by number of sales
  --eur-m2 "Hirson"         sold €/m² distribution (median computed in Python —
                            SQLite has no median and a correlated-subquery median
                            raises "misuse of aggregate function COUNT()")
  --trend                   sales count + avg €/m² per year
  --recent "Hirson" N       the N most recent sales in a town
  --vs-asking               OUR asking €/m² vs ACHIEVED sold €/m² per commune
  --disposals               our listings that left the market (sold/withdrawn)
  --sql "SELECT ..."        arbitrary read-only SQL

Town-name joining note: portal locations look like "Saint-Quentin (02100)",
"Hirson (02)" and "Liesse-Notre-Dame 02350", while DVF has "Saint-Quentin".
Matching with LIKE on the FIRST word produced nonsense ("Saint", "La", "Les"),
so both sides are normalised in Python (accents stripped, postcode/dept code
removed, punctuation → single spaces) and joined on exact equality.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import unicodedata


def norm_town(s: str) -> str:
    """Normalise a French town name for joining across sources."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\b0?\d{4,5}\b", " ", s)      # postcodes
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+\d{2}$", "", s.strip())   # trailing dept code, e.g. "hirson 02"
    return re.sub(r"\s+", " ", s).strip()


def q(con, sql, params=()):
    cur = con.execute(sql, params)
    return [d[0] for d in cur.description], cur.fetchall()


def show(title, cols, rows, widths=None):
    print(f"\n=== {title} ===")
    if not rows:
        print("  (no rows)")
        return
    if widths is None:
        widths = [max(len(str(cols[i])), max(len(str(r[i])) for r in rows))
                  for i in range(len(cols))]
    widths = [min(w, 34) for w in widths]
    print("  " + "  ".join(str(c)[:w].ljust(w) for c, w in zip(cols, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(str(v if v is not None else "-")[:w].ljust(w)
                               for v, w in zip(r, widths)))


def median(vals):
    if not vals:
        return None
    vals = sorted(vals)
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="listings.db")
    ap.add_argument("--overview", action="store_true")
    ap.add_argument("--communes", type=int, metavar="N")
    ap.add_argument("--eur-m2", metavar="TOWN")
    ap.add_argument("--trend", action="store_true")
    ap.add_argument("--recent", nargs=2, metavar=("TOWN", "N"))
    ap.add_argument("--vs-asking", action="store_true")
    ap.add_argument("--disposals", action="store_true")
    ap.add_argument("--sql", metavar="QUERY")
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ran = False

    if args.sql:
        ran = True
        show("custom query", *q(con, args.sql))

    if args.overview or not any([args.communes, args.eur_m2, args.trend, args.recent,
                                 args.vs_asking, args.disposals, args.sql]):
        ran = True
        show("OFFICIAL SALES (DVF, all Aisne)", *q(con, """
            SELECT COUNT(*) AS sales, MIN(date_mutation) AS first,
                   MAX(date_mutation) AS last, ROUND(AVG(prix_vente)) AS avg_price,
                   ROUND(AVG(eur_m2)) AS avg_eur_m2,
                   COUNT(DISTINCT commune) AS communes FROM dvf_sales_clean"""))
        _, tr = q(con, """SELECT type_local, COUNT(*), ROUND(AVG(prix_vente)),
                                 ROUND(AVG(eur_m2)) FROM dvf_sales_clean
                          GROUP BY 1 ORDER BY 2 DESC""")
        for t, n, p, m2 in tr:
            print(f"   {t}: {n:,} sales, avg €{int(p):,}, avg €{int(m2)}/m²")
        show("OUR TRACKED LISTINGS", *q(con, """
            SELECT COUNT(*) AS tracked, COUNT(DISTINCT source) AS sources,
                   SUM(status='active') AS active,
                   SUM(status<>'active') AS off_market FROM listings"""))

    if args.communes:
        ran = True
        show(f"top {args.communes} communes by sales volume",
             *q(con, """SELECT commune, COUNT(*) AS sales,
                               ROUND(AVG(prix_vente)) AS avg_price,
                               ROUND(AVG(eur_m2)) AS avg_eur_m2
                        FROM dvf_sales_clean GROUP BY 1
                        ORDER BY sales DESC LIMIT ?""", (args.communes,)))

    if args.eur_m2:
        ran = True
        want = norm_town(args.eur_m2)
        rows = con.execute("""SELECT type_local, prix_vente, surface_bati, eur_m2,
                                     date_mutation, commune
                              FROM dvf_sales_clean""").fetchall()
        by_type = {}
        for t, price, surf, m2, date, com in rows:
            if norm_town(com) != want or m2 is None:
                continue
            by_type.setdefault(t, []).append((m2, price))
        if not by_type:
            print(f"\n=== sold €/m² — {args.eur_m2} ===\n  (no sales found)")
        else:
            out = []
            for t, vals in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
                m2s = [v[0] for v in vals]
                ps = [v[1] for v in vals]
                out.append((t, len(vals), int(min(m2s)), int(median(m2s)),
                            int(sum(m2s) / len(m2s)), int(max(m2s)),
                            int(median(ps))))
            show(f"sold €/m² distribution — {args.eur_m2}",
                 ["type", "sales", "min", "median", "mean", "max", "median_price"],
                 out)

    if args.trend:
        ran = True
        show("sales + avg €/m² by year (Aisne)", *q(con, """
            SELECT substr(date_mutation,1,4) AS year, COUNT(*) AS sales,
                   ROUND(AVG(prix_vente)) AS avg_price,
                   ROUND(AVG(eur_m2)) AS avg_eur_m2
            FROM dvf_sales_clean GROUP BY 1 ORDER BY 1"""))

    if args.recent:
        town, n = norm_town(args.recent[0]), int(args.recent[1])
        ran = True
        allrows = con.execute("""SELECT date_mutation, type_local, prix_vente,
                                        surface_bati, eur_m2, commune, code_postal
                                 FROM dvf_sales_clean
                                 ORDER BY date_mutation DESC""").fetchall()
        hits = [r for r in allrows if norm_town(r[5]) == town][:n]
        show(f"most recent sales — {args.recent[0]}", 
             ["date", "type", "price", "m2", "eur_m2", "commune", "pc"], hits,
             widths=[12, 11, 10, 7, 8, 20, 6])

    if args.vs_asking:
        ran = True
        # asking side: our listings, grouped by normalised town
        ask = {}
        for loc, price, surf in con.execute("""
                SELECT location, price_eur, surface_m2 FROM listings
                WHERE surface_m2 BETWEEN 150 AND 1500
                  AND price_eur BETWEEN 10000 AND 220000"""):
            if not surf:
                continue
            t = norm_town(loc)
            if not t:
                continue
            ask.setdefault(t, []).append(price / surf)
        sold = {}
        for com, m2 in con.execute("SELECT commune, eur_m2 FROM dvf_sales_clean"):
            t = norm_town(com)
            if t:
                sold.setdefault(t, []).append(m2)
        out = []
        for t, asks in ask.items():
            sales = sold.get(t)
            if not sales or len(sales) < 3 or len(asks) < 2:
                continue
            a, s = sum(asks) / len(asks), sum(sales) / len(sales)
            out.append((t, len(asks), int(a), len(sales), int(s),
                        int(a - s), round(100 * (a - s) / s) if s else None))
        out.sort(key=lambda r: -r[5])
        show("OUR asking €/m² vs ACHIEVED sold €/m²  (gap = negotiation room)",
             ["town", "lst", "ask", "sales", "sold", "gap", "gap%"], out[:25],
             widths=[24, 4, 6, 6, 6, 6, 5])

    if args.disposals:
        ran = True
        try:
            show("our listings that LEFT the market (sold / withdrawn)",
                 *q(con, """SELECT gone_at, town, postcode, source, price_eur,
                                   surface_m2, days_on_market, gone_reason
                            FROM disposals ORDER BY gone_at DESC LIMIT 40"""))
        except sqlite3.OperationalError as e:
            print(f"\n=== disposals ===\n  table not built yet ({e})")

    if not ran:
        print("nothing to do — see --help")
    return 0


if __name__ == "__main__":
    sys.exit(main())
