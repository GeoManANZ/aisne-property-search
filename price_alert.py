"""Price-drop alert — detect genuine price reductions and format a Telegram message.

Filters out FALSE drops caused by the parser history:
  1. Malformed historical prices (>€50M, from the old broken parser).
  2. New prices that are implausibly low (<€5k) — these are detail-page
     parse artifacts, not real market drops.

Real drops are those where BOTH old and new prices are plausible (€5k–€50M)
and the drop is meaningful.

Usage:
    python price_alert.py [--days 30] [--min-pct 5] [--out FILE]
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project dir (with listings_db.py) is importable regardless of CWD.
_PROJECT_DIR = Path(__file__).resolve().parent
if _PROJECT_DIR.name == "scripts":
    # running from cron scripts copy — listings_db lives in the project dir
    _PROJECT_DIR = Path("/workspace/hermes1/projects/aisne-property-search")
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from listings_db import ListingsDB


def format_eur(v: int | None) -> str:
    return f"{v:,.0f} €".replace(",", " ") if v else "?"


def load_db():
    return ListingsDB(_PROJECT_DIR / "listings.db")


def get_real_drops(db, days: int = 30, min_pct: float = 0.0) -> list[dict]:
    """Return genuine price drops, filtering parser artifacts.

    A drop is "real" only if it meets ALL of:
      - old_price is plausible (5k–50M)
      - new_price is plausible (>=10k, <=50M) — below €10k is a parse artifact
      - the CURRENT listings table price matches the new_price (i.e. the drop
        is corroborated by the live value, not a stale/transient entry)
      - old > new

    The current-price check is the key anti-false-positive guard: a "drop"
    is only real if the listing's current price genuinely equals the lower
    figure. If the detail-scan or a bad parse briefly logged a wrong value,
    the current price won't match it and the drop is discarded.
    """
    drops = db.price_drops(days)
    # map url -> current price in the listings table
    cur = {r[0]: r[1] for r in db.conn.execute(
        "SELECT url, price_eur FROM listings").fetchall()}
    out = []
    for d in drops:
        old, new = d.get("old_price"), d.get("new_price")
        if old is None or new is None:
            continue
        # plausible old price, plausible new price
        if not (5000 <= old <= 50_000_000) or not (10000 <= new <= 50_000_000):
            continue
        # CORROBORATION: current DB price must equal the new (lower) price.
        # Otherwise this "drop" is a transient/artifact and must be ignored.
        if cur.get(d.get("url")) != new:
            continue
        pct = (old - new) / old * 100 if old else 0
        if pct < min_pct:
            continue
        d["pct"] = round(pct, 1)
        out.append(d)
    return out


def format_alert(drops: list[dict]) -> str:
    if not drops:
        return "📉 **Aucune baisse de prix détectée** (période: 30 j)"
    lines = [
        "📉 **Aisne — Baisses de prix détectées**",
        "",
    ]
    for d in drops:
        title = (d.get("title") or "Immeuble")[:45]
        loc = ""
        src = d.get("source", "")
        lines.append(
            f"• **{d['old_price']:,.0f} → {d['new_price']:,.0f} €** "
            f"({d['pct']:.0f}%) — {title}"
        )
        lines.append(f"  [{src}] {d['url']}")
    lines.append("")
    lines.append(f"_{len(drops)} baisse(s) sur la période_")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-pct", type=float, default=0.0,
                    help="Only report drops >= this % (default: all)")
    ap.add_argument("--out", default=None, help="Write message to file (for delivery)")
    ap.add_argument("--json", action="store_true", help="Print JSON drops instead of text")
    ap.add_argument("--silent-if-none", action="store_true",
                    help="Exit 0 with NO stdout if no drops (cron no_agent silent-run)")
    args = ap.parse_args()

    db = load_db()
    drops = get_real_drops(db, days=args.days, min_pct=args.min_pct)
    db.close()

    if args.json:
        print(json.dumps(drops, ensure_ascii=False, indent=2))
        return

    if args.silent_if_none and not drops:
        # no drops → silent (empty stdout = no delivery for no_agent cron)
        return

    msg = format_alert(drops)
    if args.out:
        Path(args.out).write_text(msg, encoding="utf-8")
        print(f"Wrote alert to {args.out} ({len(drops)} drops)")
    else:
        print(msg)


if __name__ == "__main__":
    main()
