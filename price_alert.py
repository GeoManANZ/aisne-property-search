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

# One-alert-per-drop state: url + new_price of every drop already delivered.
# A drop is only reported the FIRST time it is seen; subsequent runs skip it.
ALERTED_FILE = Path(os.environ.get(
    "PRICE_ALERT_STATE", "/opt/data/property_cache/aisne_alerted_drops.json"))

# Resolve the project dir (where listings_db.py lives) regardless of CWD or
# the cron-run copy location.  Two candidates:
#   1. this file's own directory (normal runs)
#   2. the canonical project path (when run from the /opt/data/scripts copy)
_CANDIDATES = [
    Path(__file__).resolve().parent,
    Path("/workspace/hermes1/projects/aisne-property-search"),
]
_PROJECT_DIR = next((p for p in _CANDIDATES if (p / "listings_db.py").exists()),
                    _CANDIDATES[0])
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from listings_db import ListingsDB


def load_db():
    return ListingsDB(_PROJECT_DIR / "listings.db")


def get_real_drops(db, days: int = None, min_pct: float = None) -> list[dict]:
    """Return genuine price drops, filtering parser artifacts.

    Defaults for `days`/`min_pct` come from config.PRICE_ALERT.  A drop is
    "real" only if it meets ALL of:
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
    import config
    if days is None:
        days = config.PRICE_ALERT["days_window"]
    if min_pct is None:
        min_pct = config.PRICE_ALERT["min_pct"]
    floor = config.PRICE_ALERT["price_floor_eur"]
    ceiling = config.PRICE_ALERT["price_ceiling_eur"]
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
        if not (5000 <= old <= ceiling) or not (floor <= new <= ceiling):
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


def load_alerted() -> dict:
    """{url: new_price} of drops already alerted."""
    try:
        return json.loads(ALERTED_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_alerted(state: dict) -> None:
    ALERTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALERTED_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                            encoding="utf-8")


def format_alert(drops: list[dict]) -> str:
    if not drops:
        return "📉 **Aucune baisse de prix détectée** (période: 30 j)"
    lines = [
        "📉 **Aisne — Baisses de prix détectées**",
        "",
    ]
    for d in drops:
        title = (d.get("title") or "Immeuble")[:45]
        loc = (d.get("location") or "")
        src = d.get("source", "")
        surf = d.get("surface_m2")
        surf_txt = f" · {surf:,.0f} m²" if surf else ""
        ppm = d.get("price_per_m2")
        ppm_txt = f" · {ppm:,.0f} €/m²" if ppm else ""
        lines.append(
            f"• **{d['old_price']:,.0f} → {d['new_price']:,.0f} €** "
            f"({d['pct']:.0f}%) — {title}{surf_txt}{ppm_txt}"
        )
        lines.append(f"  [{src}] {d['url']}")
    lines.append("")
    lines.append(f"_{len(drops)} baisse(s) sur la période_")
    return "\n".join(lines)


def main():
    import config
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=config.PRICE_ALERT["days_window"])
    ap.add_argument("--min-pct", type=float, default=config.PRICE_ALERT["min_pct"],
                    help="Only report drops >= this % (default: all)")
    ap.add_argument("--out", default=None, help="Write message to file (for delivery)")
    ap.add_argument("--json", action="store_true", help="Print JSON drops instead of text")
    ap.add_argument("--silent-if-none", action="store_true",
                    help="Exit 0 with NO stdout if no drops (cron no_agent silent-run)")
    ap.add_argument("--no-dedup", action="store_true",
                    help="Disable once-only alerting (report all drops in window)")
    args = ap.parse_args()

    db = load_db()
    drops = get_real_drops(db, days=args.days, min_pct=args.min_pct)
    db.close()

    # Once-only dedup: skip drops already delivered at this price level.
    # Keyed by url + new_price, so a FURTHER drop re-alerts.
    if args.no_dedup:
        alerted = {}
        fresh = drops
    else:
        alerted = load_alerted()
        fresh = [d for d in drops
                 if alerted.get(d.get("url")) != d.get("new_price")]
        if fresh:
            for d in fresh:
                alerted[d["url"]] = d["new_price"]
            save_alerted(alerted)
    drops = fresh

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
