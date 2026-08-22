"""Fixture tests for the two data-safety invariants.

Run: .venv/bin/python test_invariants.py
Fast (<2s), no network.  Exit 0 = all pass.
"""
import sys
import tempfile
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT))

from listings_db import validate_listing

PASS = "✓"
FAIL = "✗"
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"{PASS if cond else FAIL} {name}")


# ---------------------------------------------------------------------------
# Invariant 1: validation rejects known-bad shapes
#   Contract: hard rejects put a string starting with "reject" in errors;
#   out-of-range prices/surfaces are NULLED with an explanatory error.
# ---------------------------------------------------------------------------
# FNAIM department-wide search page masquerading as a listing
_, errs = validate_listing(
    {"url": "https://www.fnaim.fr/annonce-immobiliere/52588214/17-acheter-immeuble-aisne-2.htm",
     "source": "fnaim", "price_eur": 100_000})
check("fnaim dept search-page URL rejected",
      any(e.startswith("reject") for e in errs))

# lesiteimmo non-detail URL (category page)
_, errs = validate_listing(
    {"url": "https://www.lesiteimmo.com/acheter/maison/aisne-02",
     "source": "lesiteimmo", "price_eur": 100_000})
check("lesiteimmo category-page URL rejected",
      any(e.startswith("reject") for e in errs))

# lesiteimmo real detail URL accepted
it, errs = validate_listing(
    {"url": "https://www.lesiteimmo.com/acheter/maison-9pieces/bruyeres-et-montberault-02860/30169996",
     "source": "lesiteimmo", "price_eur": 84_700,
     "title": "Maison - 214m² - Bruyères-et-Montbérault"})
check("lesiteimmo detail URL accepted", not any(e.startswith("reject") for e in errs))

# absurd prices nulled (never stored as-is)
for bad_price in (500, 250_000_000):
    it, errs = validate_listing(
        {"url": "https://www.seloger.com/268941113/detail.htm",
         "source": "seloger", "price_eur": bad_price})
    check(f"price {bad_price} EUR nulled",
          it.get("price_eur") is None
          and any("out of range" in e for e in errs))

# unknown source hard-rejected
_, errs = validate_listing(
    {"url": "https://example.com/x/123", "source": "notasource", "price_eur": 50_000})
check("unknown source rejected", any(e.startswith("reject") for e in errs))


# ---------------------------------------------------------------------------
# Invariant 2: price-alert corroboration discards non-matching drops
# ---------------------------------------------------------------------------
from listings_db import ListingsDB

tmpdir = tempfile.mkdtemp()
db = ListingsDB(Path(tmpdir) / "test.db")  # creates its own schema

# price_history schema: (id auto, url, price_eur, surface_m2, seen_at).
# A "drop" = two consecutive history rows, newest lower.  seen_at must be
# inside the alert window (now-relative).
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
recent = (now - timedelta(days=2)).isoformat()
db.conn.execute(
    "INSERT INTO listings (url, source, title, price_eur, first_seen, last_seen,"
    " last_check, status) VALUES (?,?,?,?,?,?,?,?)",
    ("https://real.example/1", "seloger", "Real drop", 80_000,
     recent, recent, recent, "active"))
db.conn.execute(
    "INSERT INTO listings (url, source, title, price_eur, first_seen, last_seen,"
    " last_check, status) VALUES (?,?,?,?,?,?,?,?)",
    ("https://fake.example/2", "seloger", "Artifact", 150_000,
     recent, recent, recent, "active"))
for url, p in (("https://real.example/1", 137_150),
               ("https://fake.example/2", 200_000)):
    db.conn.execute(
        "INSERT INTO price_history (url, price_eur, seen_at) VALUES (?,?,?)",
        (url, p, (now - timedelta(days=3)).isoformat()))
    db.conn.execute(
        "INSERT INTO price_history (url, price_eur, seen_at) VALUES (?,?,?)",
        (url, 80_000 if "real" in url else 120_000, recent))
db.conn.commit()

from price_alert import get_real_drops
drops = get_real_drops(db)
urls = [d["url"] for d in drops]
check("corroborated drop kept", urls == ["https://real.example/1"])
check("non-corroborated artifact discarded", "https://fake.example/2" not in urls)

db.close()

# ---------------------------------------------------------------------------
print()
failed = [n for n, ok in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
