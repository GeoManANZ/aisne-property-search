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
MAX_SURFACE = config.INVESTMENT["max_surface_m2"]
PRICE_MIN = config.INVESTMENT["price_min_eur"]
PRICE_MAX = config.INVESTMENT["price_max_eur"]
TOP_N = config.INVESTMENT["top_n"]


def _loc_key(loc: str) -> tuple:
    """Normalize a location string for dup grouping: 'La Fère 02800' and
    'La Fère (02800)' must collide. Returns (town, postcode); town-only
    match with compatible postcode handled in fetch_candidates (a missing
    postcode — IAD slug locations have none — must still merge with a
    full 'Town (NNNNN)' row of the same town)."""
    import re as _re
    if not loc:
        return ("", "")
    m = _re.search(r"([A-Za-zÀ-ÿ'’\- ]+?)\s*\(?\s*(\d{2,5})?\s*\)?$", loc.strip())
    if m:
        town = _re.sub(r"\s+", " ", m.group(1)).strip().lower()
        return (town, m.group(2) or "")
    return (_re.sub(r"\s+", " ", loc.strip()).lower(), "")


def _postcode_compatible(a: str, b: str) -> bool:
    """Postcodes compatible for a probable-dup: equal, either missing, or
    one is a dept-label prefix of the other ('02' vs '02220')."""
    if not a or not b or a == b:
        return True
    return a.startswith(b) or b.startswith(a)


def fetch_candidates(db, min_surface=MIN_SURFACE, max_surface=MAX_SURFACE,
                     price_min=PRICE_MIN, price_max=PRICE_MAX,
                     group_dups=True):
    """Listings matching the investment criteria, ranked by €/m² ascending.

    group_dups=True collapses probable duplicates — same town, price and
    surface (same property listed on multiple portals / re-listed).  The
    primary (earliest-seen) row wins; the alternate URLs are attached as
    ``alt_urls`` so no link is ever hidden (user requirement).
    """
    rows = db.conn.execute(
        """
        SELECT url, source, title, price_eur, surface_m2, dpe_energy,
               location, agency, first_seen
        FROM listings
        WHERE price_eur IS NOT NULL
          AND price_eur BETWEEN ? AND ?
          AND surface_m2 IS NOT NULL
          AND surface_m2 BETWEEN ? AND ?
        ORDER BY (price_eur / surface_m2) ASC
        """,
        (price_min, price_max, min_surface, max_surface),
    ).fetchall()
    out = []
    for r in rows:
        url, source, title, price, surface, dpe, loc, agency, first_seen = r
        out.append({
            "url": url, "source": source, "title": title or "",
            "price_eur": price, "surface_m2": surface, "dpe_energy": dpe,
            "location": loc or "", "agency": agency or "",
            "first_seen": first_seen or "",
            "price_per_m2": round(price / surface, 0) if surface else None,
            "alt_urls": [],
        })
    if not group_dups:
        return out
    # Collapse probable dups: same town (normalized) + price + surface with
    # compatible postcodes. IAD slug locations carry no postcode, so a bare
    # "Hirson" row must merge with ParuVendu's "Hirson (02)".
    groups: dict[tuple, list[dict]] = {}
    for item in out:
        town, pc = _loc_key(item["location"])
        key = (town, item["price_eur"], item["surface_m2"])
        groups.setdefault(key, []).append(item)
    merged = []
    for key, items in groups.items():
        if len(items) == 1:
            merged.append(items[0])
            continue
        # postcode sanity: drop mismatched depts from the same-town group
        pcs = {_loc_key(i["location"])[1] for i in items if _loc_key(i["location"])[1]}
        if len(pcs) > 1:
            # keep only rows sharing the most common postcode prefix
            from collections import Counter
            best = Counter(p[:2] for p in pcs).most_common(1)[0][0]
            items = [i for i in items
                     if not _loc_key(i["location"])[1]
                     or _loc_key(i["location"])[1].startswith(best)]
        # earliest first_seen is the primary record
        items.sort(key=lambda x: x["first_seen"] or "")
        primary, alts = items[0], items[1:]
        primary["alt_urls"] = [
            {"url": a["url"], "source": a["source"]} for a in alts
        ]
        merged.append(primary)
    return merged


def render_markdown(cands, n: int, title: str = None) -> str:
    """Render the top-N table as GitHub-flavoured Markdown."""
    rows = cands[:n]
    lines = [
        f"# {title or 'Aisne Soissons–Laon Corridor — Investment Recommendations'}",
        "",
        f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"**Database:** {len(cands)} candidates (≥{MIN_SURFACE} m², " \
        f"≤{MAX_SURFACE:,} m², €{PRICE_MIN/1000:.0f}k–€{PRICE_MAX/1000:.0f}k)",
        "",
        "| # | Price | m² | €/m² | DPE | Town | Link |",
        "|---|-------|----|------|-----|------|------|",
    ]
    for i, r in enumerate(rows, 1):
        town = r["location"] or "–"
        dpe = r["dpe_energy"] or "–"
        alt = r.get("alt_urls") or []
        link = r['url']
        # If collapsed dup has alternate portals, show them compactly under
        # the row (all URLs kept — user requirement, never hide a link).
        if alt:
            extra = " · ".join(
                f"[{a['source']}]({a['url']})" for a in alt[:3]
            )
            if len(alt) > 3:
                extra += f" · +{len(alt)-3} more"
            lines.append(
                f"| {i} | €{r['price_eur']:,} | {r['surface_m2']:,.0f} | "
                f"**{r['price_per_m2']:,.0f}** | {dpe} | {town} | "
                f"[{r['source']}]({link}) ⧉ {extra} |"
            )
        else:
            lines.append(
                f"| {i} | €{r['price_eur']:,} | {r['surface_m2']:,.0f} | "
                f"**{r['price_per_m2']:,.0f}** | {dpe} | {town} | {link} |"
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
