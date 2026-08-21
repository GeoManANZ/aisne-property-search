"""Seed the listings DB from the existing scan JSON (one-time migration).

The scraper's batch runs write structured data to scans/<id>/results.json.
This loads every scan's results and upserts them into listings.db so the
DB starts with the full history we've already gathered.  Safe to re-run —
upserts are idempotent.
"""
import json
import sys
from pathlib import Path

from listings_db import ListingsDB

SCANS_DIR = Path(__file__).parent / "scans"
DB_PATH = Path(__file__).parent / "listings.db"


# URLs that are SEARCH/RESULTS pages, not individual listings — skip these.
_SEARCH_URL_PATTERNS = [
    "recherche", "recherche-immo", "liste-annonces", "/annonces/",
    "superimmo.com/vente/immeuble", "zilek.com/immeuble-a-vendre",
    "/vente/immeuble/soissons-02200/", "/vente/immeuble/laon-02000/",
    "/vente/immeuble/aisne-02/",
]


def _is_search_page(url: str) -> bool:
    """True if the URL is a search/filter page rather than a single listing."""
    u = url.lower()
    return any(p in u for p in _SEARCH_URL_PATTERNS)


def load_scan_results() -> list[dict]:
    """Collect listing dicts from scanned_details.json (the rich scan dump).

    The per-run results.json files hold single-URL diagnostics; the full
    multi-listing data with price/surface/DPE is in scanned_details.json.
    """
    all_listings = []
    # 1) scanned_details.json files (rich data)
    for sf in sorted(SCANS_DIR.rglob("scanned_details.json")):
        try:
            data = json.loads(sf.read_text())
        except Exception as e:
            print(f"  skip {sf}: {e}")
            continue
        items = data if isinstance(data, list) else data.get("scanned_details", [])
        for it in items:
            d = it.get("data", {}) if isinstance(it, dict) else {}
            url = it.get("url") or d.get("url")
            if not url or _is_search_page(url):
                continue
            all_listings.append({
                "url": url,
                "source": it.get("source") or d.get("source"),
                "title": d.get("title"),
                "price_eur": d.get("price_eur"),
                "surface_m2": d.get("surface_m2"),
                "dpe_energy": d.get("dpe_energy"),
                "location": d.get("location"),
                "agency": d.get("agency"),
                "description": d.get("description_snippet"),
            })
    # 2) results.json files (single-URL runs, e.g. Orpi)
    for rf in sorted(SCANS_DIR.rglob("results.json")):
        try:
            data = json.loads(rf.read_text())
        except Exception:
            continue
        items = data if isinstance(data, list) else data.get("scanned_details", [])
        for it in items:
            d = it.get("data", {}) if isinstance(it, dict) else {}
            url = it.get("url") or d.get("url")
            if not url or _is_search_page(url) or any(x["url"] == url for x in all_listings):
                continue  # dedupe against scanned_details
            all_listings.append({
                "url": url,
                "source": it.get("source") or d.get("source"),
                "title": d.get("title"),
                "price_eur": d.get("price_eur"),
                "surface_m2": d.get("surface_m2"),
                "dpe_energy": d.get("dpe_energy"),
                "location": d.get("location"),
                "agency": d.get("agency"),
                "description": d.get("description_snippet"),
            })
    return all_listings


def main():
    listings = load_scan_results()

    # Manually add high-value listings that came from ad-hoc live scrapes
    # not captured in the JSON scans (e.g. the cracked Orpi page).
    ORPI_LAON = {
        "url": "https://www.orpi.com/annonce-vente-immeuble-t5-laon-02000-8ab73979-6aa3-41bd-bb2f-89630451e57b/",
        "source": "orpi", "title": "Immeuble de rapport T5 - Laon",
        "price_eur": 194900, "surface_m2": 180,
        "location": "Laon", "agency": "Orpi",
        "description": "Immeuble de rapport, bon état, 5 pièces, copropriété de 4 lots. "
                       "Réservé par : vendu via agence. Prix honoraires vendeur.",
    }
    if not any(x["url"] == ORPI_LAON["url"] for x in listings):
        listings.append(ORPI_LAON)
    else:
        # URL already present (e.g. from a single-URL results.json with no
        # source) — enrich it with the source/title/price fields we know.
        for x in listings:
            if x["url"] == ORPI_LAON["url"]:
                x.update({k: v for k, v in ORPI_LAON.items() if v})

    print(f"Loaded {len(listings)} listings from scan results")

    db = ListingsDB(DB_PATH)
    db.bulk_upsert(listings)
    print(f"DB now holds {db.count()} unique listings")

    # Show a quick summary
    rows = db.query(order_by="price_eur ASC", limit=10)
    print("\nCheapest 10 in DB:")
    for r in rows:
        pp = round(r["price_eur"] / r["surface_m2"]) if r["price_eur"] and r["surface_m2"] else "?"
        pe = f"{r['price_eur']:,}" if r["price_eur"] else "?"
        sm = f"{r['surface_m2']:g}" if r["surface_m2"] else "?"
        print(f"  {str(pp):>5} €/m² | {pe:>9} | {sm:>5} m² | "
              f"{r['source'] or '?':8s} | {r['url']}")
    db.close()


if __name__ == "__main__":
    main()
