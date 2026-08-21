# Aisne Property Search — Aisne (02) mixed-use / immeuble de rapport pipeline

Multi-source scraper for commercial/residential investment buildings in the
Soissons–Laon corridor, Aisne (02), France. Targets: **< €220k, ≥ 150 m²**,
mixed-use / immeuble de rapport.

## Sources

| Source | Method | Notes |
|--------|--------|-------|
| **SeLoger** | BFF API (`serp-bff/search` POST + `classifiedList` GET) via Camoufox + Webshare FR sticky proxy | DataDome-protected; earns cookie via browser, then fetches structured JSON. Definitive SeLoger path. |
| **FNAIM** | HTML parser (`french_property_parsers.py`) | direct HTTP |
| **IAD France** | HTML parser (URL-slug surface/town) | direct HTTP |
| **ParuVendu** | lxml card-level parser | direct HTTP |

## Pipeline

```
seloger_api_sweep.py  ──┐
french_property_parsers.py (ladder) ──┼──► listings_db.py (SQLite) ──► price_alert.py ──► Telegram
```

- `listings.db` — SQLite, URL-keyed upserts, append-only `price_history` for drops
- `validate_listing()` — ingest guard (URL shape, price/surface ranges, junk titles)
- `price_alert.py` — cron job, silent when no genuine drops (corroboration-guarded)

## Setup

```bash
cp .env.example .env   # fill in Webshare creds (SeLoger path)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python seloger_api_sweep.py            # SeLoger (BFF API, browser)
.venv/bin/python french_property_parsers.py ladder --sources fnaim iad paruvendu
.venv/bin/python price_alert.py --json           # genuine price drops
```

## Key lessons (see `LEARNINGS.md`)

- SeLoger SPA ignores query-string pagination — use the BFF API.
- Detail-page price parses are unreliable (hidden embedded numbers) — search-card
  prices are authoritative; alerts corroborate against the current DB price.
- Never trust a price change from a single scrape source.
