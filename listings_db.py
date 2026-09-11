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
    "location", "agency", "description", "features", "tags",
    "first_seen", "last_seen",
]

# ---------------------------------------------------------------------------
# Data validation — reject bad rows BEFORE they hit the DB.
# ---------------------------------------------------------------------------
# Each source has a URL shape that points to an INDIVIDUAL property.  Rows
# that don't match (town/category pages, generic search URLs, JS-fragment
# titles, absurd prices) are the junk that polluted earlier rankings.

_VALID_SOURCES = {"fnaim", "iad", "paruvendu", "seloger", "orpi", "superimmo", "logicimmo", "lesiteimmo"}
_VALID_DPE = {"A", "B", "C", "D", "E", "F", "G"}


def _url_is_individual(url: str, source: str) -> bool:
    """True if `url` points to an individual property for that source."""
    if not url:
        return False
    if source == "paruvendu":
        # detail URL ends in a base36 ID under /immeuble/ OR /maison/
        import re
        return bool(re.search(r"/(?:immeuble|maison)/([A-Z0-9]{17,22})$", url))
    if source == "fnaim":
        import re
        # detail URL has a numeric ad id: /annonce-immobiliere/<digits>/<slug>
        # BUT department-wide search pages share that shape, e.g.
        #   .../52588214/17-acheter-immeuble-aisne-2.htm   ← NOT a property
        #   .../123456/1-acheter-maison-aisne-02.htm       ← NOT a property
        # Real detail slugs end with <town>-<postcode>.htm (5-digit postcode);
        # dept search pages end with a 1-2 digit department number.
        if re.search(r"/annonce-immobiliere/\d+/(?:\d+-)?acheter-(?:immeuble|maison)-[^/]*-\d{1,2}\.htm$", url):
            return False
        return bool(re.search(r"/annonce-immobiliere/\d+/", url))
    if source == "iad":
        # detail URL: /annonce/<immeuble|maison>-vente-<town>-<N>m2/r<id>
        import re
        return bool(re.search(r"/annonce/(?:immeuble|maison)-vente-", url))
    if source == "lesiteimmo":
        import re
        return bool(re.match(
            r"^https://www\.lesiteimmo\.com/acheter/(?:immeuble|maison(?:-\d+pieces)?)/[^/]+/\d{6,9}$",
            url))
    if source == "seloger":
        # canonical detail: /<digits>/detail.htm OR a slug detail URL OR a
        # promoted /wl-cdp/<id> ad (real listings, non-canonical path)
        import re
        return (bool(re.search(r"/(\d{6,12})/detail\.htm", url))
                or "/annonce/" in url
                or "/wl-cdp/" in url)
    return True  # other sources: no strict rule


from config import is_land_not_building  # noqa: E402  (script-style module)


def validate_listing(item: dict) -> tuple[dict, list[str]]:
    """Sanity-check a parsed listing. Returns (cleaned_item, errors).

    Errors are non-fatal field-level problems that are corrected; if a row is
    fundamentally junk (wrong URL shape, nonsense price), errors include a
    'reject' marker so callers can drop it.
    """
    errors = []
    it = dict(item)
    source = it.get("source")

    # URL must point to an individual property
    url = it.get("url") or ""
    if not _url_is_individual(url, source):
        errors.append(f"reject: bad URL shape for {source}: {url}")
        return it, errors

    # price sanity
    p = it.get("price_eur")
    if p is not None:
        try:
            p = int(p)
        except (TypeError, ValueError):
            errors.append("price_eur non-numeric -> None")
            p = None
        if p is not None and (p < 5000 or p > 50_000_000):
            errors.append(f"price_eur {p} out of range -> None")
            p = None
        it["price_eur"] = p

    # surface sanity
    s = it.get("surface_m2")
    if s is not None:
        try:
            s = float(s)
        except (TypeError, ValueError):
            s = None
        if s is not None and (s < 5 or s > 100_000):
            errors.append(f"surface_m2 {s} out of range -> None")
            s = None
        it["surface_m2"] = s

    # DPE sanity
    dpe = it.get("dpe_energy")
    if dpe:
        d = str(dpe).strip().upper()
        it["dpe_energy"] = d if d in _VALID_DPE else None
        if d not in _VALID_DPE:
            errors.append(f"dpe_energy '{dpe}' invalid -> None")

    # title: reject JS-fragment / photo-count junk
    title = it.get("title") or ""
    tl = title.lower()
    if any(x in tl for x in ("const imageelement", "function(", "voir d'autres",
                             "&nbsp;", "document.getelementbyid")):
        errors.append("title is DOM/JS junk -> cleared")
        it["title"] = ""

    # Land sold as a building — reject at the door (config.is_land_not_building).
    # Runs AFTER the junk-title cleanup: a DOM-junk title would otherwise hide
    # the land signal. Deliberately high-precision — any building word wins, so
    # "maison avec terrain" is never dropped.
    if is_land_not_building(it.get("title"), it.get("surface_m2")):
        errors.append(f"reject: land/terrain sold as a building: {title[:60]!r}")
        return it, errors

    # source must be known
    if source not in _VALID_SOURCES:
        errors.append(f"unknown source '{source}' -> reject")
        errors.append("reject")

    return it, errors


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
        if "features" not in cols:
            c.execute("ALTER TABLE listings ADD COLUMN features TEXT")
        if "tags" not in cols:
            c.execute("ALTER TABLE listings ADD COLUMN tags TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status)")
        c.commit()

    def upsert_listing(self, *, url: str, source=None, title=None, price_eur=None,
                       surface_m2=None, dpe_energy=None, location=None,
                       agency=None, description=None, features=None, tags=None,
                       status="active", now: str | None = None):
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
                                  features, tags,
                                  first_seen, last_seen, last_check, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
                source      = COALESCE(excluded.source, listings.source),
                title       = COALESCE(excluded.title, listings.title),
                price_eur   = COALESCE(excluded.price_eur, listings.price_eur),
                surface_m2  = COALESCE(excluded.surface_m2, listings.surface_m2),
                dpe_energy  = COALESCE(excluded.dpe_energy, listings.dpe_energy),
                location    = COALESCE(excluded.location, listings.location),
                agency      = COALESCE(excluded.agency, listings.agency),
                description = COALESCE(excluded.description, listings.description),
                features    = COALESCE(excluded.features, listings.features),
                tags        = COALESCE(excluded.tags, listings.tags),
                last_seen   = excluded.last_seen,
                last_check  = excluded.last_check,
                status      = excluded.status
            """,
            (url, source, title, price_eur, surface_m2, dpe_energy,
             location, agency, description, features, tags,
             first_seen, now, now, status),
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

    def bulk_upsert_validated(self, listings: list[dict], now: str | None = None,
                              reject_log=None) -> tuple[int, int]:
        """Validate + upsert many listings. Returns (accepted, rejected)."""
        accepted = 0
        rejected = 0
        for item in listings:
            cleaned, errors = validate_listing(item)
            if any(e.startswith("reject") for e in errors):
                rejected += 1
                if reject_log is not None:
                    reject_log.append((item.get("url"), item.get("source"), errors))
                continue
            self.upsert_listing(now=now, **{k: v for k, v in cleaned.items()
                                            if k in _FIELDS})
            accepted += 1
        self.conn.commit()
        return accepted, rejected

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
            SELECT ph.url, l.title, l.source, l.status, l.surface_m2,
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
        # dedupe per URL (keep the latest drop); compute €/m² where possible
        seen: set[str] = set()
        out = []
        for r in rows:
            if r["url"] not in seen:
                seen.add(r["url"])
                surf = r.get("surface_m2")
                r["price_per_m2"] = (r["new_price"] / surf) if (surf and surf > 0) else None
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
    db = ListingsDB(Path(__file__).parent / "listings.db")
    db.upsert_listing(url="https://example.com/test1", source="fnaim",
                      title="Immeuble test", price_eur=100000, surface_m2=200,
                      location="Laon")
    db.upsert_listing(url="https://example.com/test1", source="fnaim",
                      price_eur=95000)  # price drop → logged
    print(f"Count: {db.count()}")
    print("Rows:", db.query())
    print("Price drops:", db.price_drops())
    db.close()
