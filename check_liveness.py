#!/usr/bin/env python3
"""Liveness + disposition recorder — did our tracked listings sell, or vanish?

THE BUG THIS FIXES
The pipeline had NO stale-listing guard. Every row stayed status='active'
forever, so reports kept surfacing properties whose ads were long gone. Confirmed
by hand 2026-09-10: of the top-25 listings the report led with, several SeLoger
ads returned "Annonce supprimée"; iad Hirson 350 m² showed "vendu"; yet the DB
still had all 3,568 rows as active.

WHAT IT DOES
Probes each tracked listing's detail page, then records the outcome:
  * transitions are appended to `listing_events` (append-only audit trail)
  * a listing that left the market lands in `disposals` with days_on_market,
    price, €/m², town and the exact evidence string
  * `listings.status` becomes 'gone' (+gone_at/gone_reason) so reports can
    exclude it — a genuinely sold property is a DATA POINT, not a lost lead

Disposition reasons
  sold        page says vendu / sous compromis / sous offre
  withdrawn   page says annonce supprimée / n'est plus disponible
  dead_url    404/410 — ad deleted outright
  blocked     403/429 bot wall — NOT proof of life, left untouched

Direct egress only (zero Webshare cost). SeLoger bot-walls direct requests:
those come back 'blocked' and are left alone rather than being mislabelled.
Pass --seloger-via-extract to confirm them through the extraction backend.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

DB = "listings.db"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Ordered: the first match wins, so specific sold/under-offer phrases are
# checked before the generic "no longer available" ones.
STALE_PATTERNS = [
    (r"\bvendu(?:e|s|es)?\b", "sold"),
    (r"sous (?:offre|compromis)", "sold"),
    (r"offre acceptée", "sold"),
    (r"compromis (?:de vente )?signé", "sold"),
    (r"bien (?:vendu|réservé)", "sold"),
    (r"annonce (?:expirée|supprimée|retirée|introuvable)", "withdrawn"),
    (r"n['’]est plus (?:disponible|à la vente|en vente)", "withdrawn"),
    (r"plus disponible", "withdrawn"),
    (r"cette annonce n['’]existe plus", "withdrawn"),
    (r"\bretir[ée]e? de la vente\b", "withdrawn"),
    (r"page introuvable", "withdrawn"),
]
STALE_RE = [(re.compile(p, re.I), lbl) for p, lbl in STALE_PATTERNS]

SCHEMA = """
CREATE TABLE IF NOT EXISTS listing_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    source TEXT,
    observed_at TEXT NOT NULL,
    event TEXT NOT NULL,              -- seen_live | gone | price_changed | blocked
    price_eur REAL,
    previous_price_eur REAL,
    http_status INTEGER,
    marker TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_le_url ON listing_events(url);
CREATE INDEX IF NOT EXISTS ix_le_obs ON listing_events(observed_at);

CREATE TABLE IF NOT EXISTS disposals (
    url TEXT PRIMARY KEY,
    source TEXT, town TEXT, postcode TEXT,
    price_eur REAL, surface_m2 REAL, eur_m2 REAL,
    first_seen TEXT, last_alive_at TEXT, gone_at TEXT, gone_reason TEXT,
    days_on_market INTEGER, marker TEXT
);
CREATE INDEX IF NOT EXISTS ix_disp_gone ON disposals(gone_at);
CREATE INDEX IF NOT EXISTS ix_disp_town ON disposals(town);
"""

NEW_COLS = [
    ("gone_at", "TEXT"),
    ("gone_reason", "TEXT"),
    ("last_alive_at", "TEXT"),
    ("http_status", "INTEGER"),
    ("liveness_checked_at", "TEXT"),
]


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(listings)")}
    for col, typ in NEW_COLS:
        if col not in have:
            db.execute(f"ALTER TABLE listings ADD COLUMN {col} {typ}")


def probe(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(400_000).decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return {"result": "gone", "reason": "dead_url", "http": e.code,
                    "marker": f"HTTP {e.code}"}
        return {"result": "blocked", "reason": None, "http": e.code,
                "marker": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"result": "error", "reason": None, "http": None,
                "marker": type(e).__name__}

    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body,
                  flags=re.S | re.I)
    for rx, label in STALE_RE:
        m = rx.search(text)
        if m:
            return {"result": "gone", "reason": label, "http": status,
                    "marker": f"{label}: {m.group(0)!r}"}
    return {"result": "live", "reason": None, "http": status, "marker": ""}


def postcode_of(loc: str) -> str:
    m = re.search(r"\b(\d{5})\b", loc or "")
    return m.group(1) if m else ""


def record(db: sqlite3.Connection, row, res: dict, now: str) -> str:
    """Apply one probe result. Returns the action taken (for reporting)."""
    url, source = row["url"], row["source"]
    prev_status = row["status"] or "active"

    if res["result"] == "live":
        # log a 'seen_live' event only on a transition back, or weekly, so the
        # event table does not balloon with 3,500 identical rows per run
        last = db.execute(
            "SELECT event, observed_at FROM listing_events WHERE url=? "
            "ORDER BY id DESC LIMIT 1", (url,)).fetchone()
        weekly = (not last) or (last[0] != "seen_live") or \
                 (last[1][:10] < now[:10])
        if weekly:
            db.execute(
                "INSERT INTO listing_events (url,source,observed_at,event,price_eur,"
                "http_status,marker,note) VALUES (?,?,?,?,?,?,?,?)",
                (url, source, now, "seen_live", row["price_eur"], res["http"],
                 "", "still on the market"))
        db.execute(
            "UPDATE listings SET status='active', last_alive_at=?, http_status=?, "
            "liveness_checked_at=? WHERE url=?",
            (now, res["http"], now, url))
        return "live"

    if res["result"] in ("blocked", "error"):
        db.execute("UPDATE listings SET http_status=?, liveness_checked_at=? WHERE url=?",
                   (res["http"], now, url))
        return res["result"]

    # gone
    reason = res["reason"] or "withdrawn"
    days = None
    if row["first_seen"]:
        try:
            d0 = datetime.fromisoformat(row["first_seen"].replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - d0).days
        except ValueError:
            days = None
    eur_m2 = round(row["price_eur"] / row["surface_m2"], 1) \
        if row["price_eur"] and row["surface_m2"] else None
    last_alive = row["last_alive_at"] or row["last_seen"]
    db.execute("""
        INSERT OR REPLACE INTO disposals
        (url,source,town,postcode,price_eur,surface_m2,eur_m2,first_seen,
         last_alive_at,gone_at,gone_reason,days_on_market,marker)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (url, source, row["location"], postcode_of(row["location"]),
         row["price_eur"], row["surface_m2"], eur_m2, row["first_seen"],
         last_alive, now, reason, days, res["marker"]))
    db.execute("""
        UPDATE listings SET status='gone', gone_at=?, gone_reason=?,
               http_status=?, liveness_checked_at=? WHERE url=?""",
        (now, reason, res["http"], now, url))
    if prev_status != "gone":
        db.execute(
            "INSERT INTO listing_events (url,source,observed_at,event,price_eur,"
            "http_status,marker,note) VALUES (?,?,?,?,?,?,?,?)",
            (url, source, now, "gone", row["price_eur"], res["http"],
             res["marker"], f"left market ({reason})"))
    return f"gone:{reason}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40,
                    help="how many listings to check (cheapest €/m² first)")
    ap.add_argument("--all-criteria", action="store_true",
                    help="check every listing matching the investment criteria")
    ap.add_argument("--recheck-gone", action="store_true",
                    help="also re-probe rows already marked gone")
    ap.add_argument("--json", help="write results here")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    db.commit()

    where = "WHERE url IS NOT NULL AND surface_m2 BETWEEN 150 AND 1500 " \
            "AND price_eur BETWEEN 10000 AND 220000"
    if not args.recheck_gone:
        where += " AND COALESCE(status,'active')='active'"
    sql = (f"SELECT url, source, status, price_eur, surface_m2, location, "
           f"first_seen, last_seen, last_alive_at FROM listings {where} "
           f"ORDER BY (price_eur / NULLIF(surface_m2,0)) ASC")
    if not args.all_criteria:
        sql += f" LIMIT {args.n}"
    rows = db.execute(sql).fetchall()
    print(f"probing {len(rows)} listing(s)...", flush=True)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results, actions = [], {}
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(probe, r["url"]): r for r in rows}
        for f in cf.as_completed(futs):
            r = futs[f]
            res = f.result()
            act = record(db, r, res, now)
            db.commit()
            actions[act] = actions.get(act, 0) + 1
            results.append({"url": r["url"], "source": r["source"],
                            "town": r["location"], "price": r["price_eur"], **res})
            if not args.quiet:
                mark = {"live": "OK   ", "gone": "GONE "}.get(res["result"], "---- ")
                print(f"  {mark} {r['source']:11s} {str(r['location'])[:24]:24s} "
                      f"€{r['price_eur'] or 0:<7} {act} {res['marker'][:38]}")

    print("\n=== actions ===")
    for k, v in sorted(actions.items(), key=lambda kv: -kv[1]):
        print(f"  {k:16s} {v}")
    n_disp = db.execute("SELECT COUNT(*) FROM disposals").fetchone()[0]
    n_gone = db.execute("SELECT COUNT(*) FROM listings WHERE status='gone'").fetchone()[0]
    n_ev = db.execute("SELECT COUNT(*) FROM listing_events").fetchone()[0]
    print(f"\ndisposals recorded: {n_disp}   listings marked gone: {n_gone}   events: {n_ev}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=1)
        print(f"results -> {args.json}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
