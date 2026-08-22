"""Aisne property recommendations — top N by €/m², Telegram-ready Markdown.

Reads listings.db, applies the investor criteria (mixed-use / immeuble,
>= 150 m², €10k–€220k), ranks by €/m² ascending, and writes a Markdown
report for Telegram MEDIA delivery (or stdout).

Usage:
    python recommendations_report.py [--n 20] [--out PATH.md]
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_DIR = Path(__file__).resolve().parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from listings_db import ListingsDB
import config

MIN_SURFACE = config.INVESTMENT["min_surface_m2"]
PRICE_MIN = config.INVESTMENT["price_min_eur"]
PRICE_MAX = config.INVESTMENT["price_max_eur"]
TOP_N = config.INVESTMENT["top_n"]


def fetch_candidates(db, min_surface=MIN_SURFACE, price_min=PRICE_MIN,
                     price_max=PRICE_MAX):
    """Listings matching the investment criteria, ranked by €/m² ascending."""
    rows = db.conn.execute(
        """
        SELECT url, source, title, price_eur, surface_m2, dpe_energy,
               location, agency, first_seen
        FROM listings
        WHERE price_eur IS NOT NULL
          AND price_eur BETWEEN ? AND ?
          AND surface_m2 IS NOT NULL
          AND surface_m2 >= ?
        ORDER BY (price_eur / surface_m2) ASC
        """,
        (price_min, price_max, min_surface),
    ).fetchall()
    out = []
    for r in rows:
        url, source, title, price, surface, dpe, loc, agency, first_seen = r
        out.append({
            "url": url, "source": source, "title": title or "",
            "price_eur": price, "surface_m2": surface, "dpe_energy": dpe,
            "location": loc or "", "agency": agency or "",
            "price_per_m2": round(price / surface, 0) if surface else None,
        })
    return out


def render_markdown(cands, n: int, title: str = None) -> str:
    """Render the top-N table as GitHub-flavoured Markdown."""
    rows = cands[:n]
    lines = [
        f"# {title or 'Aisne Soissons–Laon Corridor — Investment Recommendations'}",
        "",
        f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"**Database:** {len(cands)} candidates (≥{MIN_SURFACE} m², "
        f"€{PRICE_MIN/1000:.0f}k–€{PRICE_MAX/1000:.0f}k)",
        "",
        "| # | Price | m² | €/m² | DPE | Town | Link |",
        "|---|-------|----|------|-----|------|------|",
    ]
    for i, r in enumerate(rows, 1):
        town = r["location"] or "–"
        dpe = r["dpe_energy"] or "–"
        lines.append(
            f"| {i} | €{r['price_eur']:,} | {r['surface_m2']:,.0f} | "
            f"**{r['price_per_m2']:,.0f}** | {dpe} | {town} | {r['url']} |"
        )
    if not rows:
        lines.append("_(no listings match the criteria)_")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=TOP_N)
    ap.add_argument("--out", default=None, help="Write Markdown to file")
    ap.add_argument("--title", default=None)
    args = ap.parse_args()

    db = ListingsDB(_PROJECT_DIR / "listings.db")
    cands = fetch_candidates(db)
    db.close()

    md = render_markdown(cands, args.n, args.title)
    if args.out:
        out = Path(args.out)
        out.write_text(md, encoding="utf-8")
        print(f"Wrote {len(cands[:args.n])} recommendations → {out}")
    else:
        print(md)


if __name__ == "__main__":
    main()
