"""
Listings database — persistent SQLite store for scraped French properties.

Why SQLite: single-file, zero-server, survives across scrapes, queryable,
and trivially backed up / exported.  Every listing URL is the primary key so
re-scraping the same property UPSERTS (updates fields, bumps last_seen)
instead of creating duplicates — so the DB naturally accumulates a history
of what we've seen and when each listing was last verified live.

Schema philosophy:
  - `listings`      — one row per unique listing URL (the live feed)
  - `price_history` — append-only log of price changes per listing, so we can
                      detect price drops (great for "price reduced" alerts)
  - source, location, DPE etc. are denormalised onto listings for easy
                    filtering in the report step.

Usage (from any scraper module):
    from listings_db import ListingsDB
    db = ListingsDB("/path/to/listings.db")
    db.upsert_listing(url=..., source=..., price_eur=..., surface_m2=..., ...)
    db.report(region="Aisne")
    db.close()

Thread-safety: SQLite serialises writes; `check_same_thread=False` lets us
use it from Playwright callbacks.  Use a short busy_timeout so concurrent
scrapes don't deadlock.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone


# Columns shared by every upsert.  All are optional; only `url` is required.
_FIELDS = [
    "url", "source", "title", "price_eur", "surface_m2", "dpe_energy",
    "location", "agency", "description", "first_seen", "last_seen",
]


class ListingsDB:
    """Thin, safe wrapper around a SQLite file for property listings."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False allows use from multiple scrape threads;
        # busy_timeout avoids "database is locked" under concurrency.
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 10000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self):
        c = self.conn
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                url          TEXT PRIMARY KEY,
                source       TEXT,
                title        TEXT,
                price_eur    INTEGER,
                surface_m2   REAL,
                dpe_energy   TEXT,
                location     TEXT,
                agency       TEXT,
                description  TEXT,
                first_seen   TEXT,
                last_seen    TEXT,
                last_check   TEXT,
                status       TEXT DEFAULT 'active'
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS price_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                url        TEXT NOT NULL,
                price_eur  INTEGER,
                surface_m2 REAL,
                seen_at    TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price_eur)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_listings_surface ON listings(surface_m2)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pricehist_url ON price_history(url)")
        # Backfill new columns on existing databases (ALTER IF NOT EXISTS isn't
        # standard SQLite, so check pragma first) — BEFORE creating indexes
        # that reference the new columns.
        cols = [r[1] for r in c.execute("PRAGMA table_info(listings)")]
        if "last_check" not in cols:
            c.execute("ALTER TABLE listings ADD COLUMN last_check TEXT")
        if "status" not in cols:
            c.execute("ALTER TABLE listings ADD COLUMN status TEXT DEFAULT 'active'")
        c.execute("CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status)")
        c.commit()

    def upsert_listing(self, *, url: str, source=None, title=None, price_eur=None,
                       surface_m2=None, dpe_energy=None, location=None,
                       agency=None, description=None, status="active",
                       now: str | None = None):
        """Insert a new listing or update an existing one (keyed by URL).

        Tracks first_seen vs last_seen, sets last_check on every upsert, and
        appends to price_history whenever the price differs from the last
        known value for that URL.  `status` is 'active' by default; callers
        can pass 'gone' or 'sold' when a detail fetch shows the ad is dead.
        """
        now = now or datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "SELECT price_eur, first_seen, status FROM listings WHERE url = ?", (url,)
        )
        row = cur.fetchone()
        first_seen = row[1] if row else now
        prev_status = row[2] if row else None

        self.conn.execute(
            """
            INSERT INTO listings (url, source, title, price_eur, surface_m2,
                                  dpe_energy, location, agency, description,
                                  first_seen, last_seen, last_check, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
                source      = COALESCE(excluded.source, listings.source),
                title       = COALESCE(excluded.title, listings.title),
                price_eur   = COALESCE(excluded.price_eur, listings.price_eur),
                surface_m2  = COALESCE(excluded.surface_m2, listings.surface_m2),
                dpe_energy  = COALESCE(excluded.dpe_energy, listings.dpe_energy),
                location    = COALESCE(excluded.location, listings.location),
                agency      = COALESCE(excluded.agency, listings.agency),
                description = COALESCE(excluded.description, listings.description),
                last_seen   = excluded.last_seen,
                last_check  = excluded.last_check,
                status      = excluded.status
            """,
            (url, source, title, price_eur, surface_m2, dpe_energy,
             location, agency, description, first_seen, now, now, status),
        )

        # Status transition → also log to price_history when a listing goes
        # gone/sold? No — that table is price-only.  Keep it simple.

        # Price-change logging.  Log the initial price on first insert too,
        # so price_drops() can compare against a real prior value.
        if price_eur is not None and (row is None or row[0] is None or price_eur != row[0]):
            self.conn.execute(
                "INSERT INTO price_history (url, price_eur, surface_m2, seen_at)"
                " VALUES (?,?,?,?)",
                (url, price_eur, surface_m2, now),
            )

        self.conn.commit()

    def bulk_upsert(self, listings: list[dict], now: str | None = None):
        """Upsert many listing dicts in one transaction (faster than per-row)."""
        now = now or datetime.now(timezone.utc).isoformat()
        for item in listings:
            self.upsert_listing(now=now, **{k: v for k, v in item.items() if k in _FIELDS})
        self.conn.commit()

    def query(self, *, source=None, min_price=None, max_price=None,
              min_surface=None, location=None, order_by="price_eur ASC",
              limit: int | None = None) -> list[dict]:
        """Flexible read query — the report/briefing step uses this."""
        q = "SELECT * FROM listings WHERE 1=1"
        args = []
        if source:
            q += " AND source = ?"; args.append(source)
        if min_price is not None:
            q += " AND price_eur >= ?"; args.append(min_price)
        if max_price is not None:
            q += " AND price_eur <= ?"; args.append(max_price)
        if min_surface is not None:
            q += " AND surface_m2 >= ?"; args.append(min_surface)
        if location:
            q += " AND location LIKE ?"; args.append(f"%{location}%")
        q += f" ORDER BY {order_by}"
        if limit:
            q += " LIMIT ?"; args.append(limit)
        cur = self.conn.execute(q, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def price_drops(self, days: int = 30) -> list[dict]:
        """Listings whose price changed downward within the last `days` days.

        Only considers price_history entries within the window, so a drop
        from 3 months ago doesn't keep alerting forever.  Returns the most
        recent drop per URL.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self.conn.execute(
            """
            SELECT ph.url, l.title, l.source, l.status,
                   ph.price_eur AS new_price,
                   (SELECT price_eur FROM price_history ph2
                    WHERE ph2.url = ph.url AND ph2.id < ph.id
                    ORDER BY ph2.id DESC LIMIT 1) AS old_price,
                   ph.seen_at
            FROM price_history ph JOIN listings l ON l.url = ph.url
            WHERE ph.seen_at >= ?
              AND ph.price_eur < (
                    SELECT price_eur FROM price_history ph2
                    WHERE ph2.url = ph.url AND ph2.id < ph.id
                    ORDER BY ph2.id DESC LIMIT 1)
            ORDER BY ph.seen_at DESC
            """,
            (cutoff,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        # dedupe per URL (keep the latest drop)
        seen: set[str] = set()
        out = []
        for r in rows:
            if r["url"] not in seen:
                seen.add(r["url"])
                out.append(r)
        return out

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]

    def close(self):
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    # Self-test
    db = ListingsDB("/workspace/hermes1/projects/aisne-property-search/listings.db")
    db.upsert_listing(url="https://example.com/test1", source="fnaim",
                      title="Immeuble test", price_eur=100000, surface_m2=200,
                      location="Laon")
    db.upsert_listing(url="https://example.com/test1", source="fnaim",
                      price_eur=95000)  # price drop → logged
    print(f"Count: {db.count()}")
    print("Rows:", db.query())
    print("Price drops:", db.price_drops())
    db.close()
