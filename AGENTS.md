# AGENTS.md — Aisne Property Search

Agent-facing guide for working on this repo. Read this first; deeper context in
PROJECT.md (ops), LEARNINGS.md (lessons), AI-ASSISTANT-PROMPT.md (background).

## What this project does
Scrapes French property listings (target: Aisne dept 02, mixed-use/immeuble +
maison, ≥150 m², €10k–€220k) from 6 sources into `listings.db`, with price-drop
detection and scheduled Telegram reports.

## Environment
- **Always** use `.venv/bin/python` (never system python). Install deps:
  `uv pip install --python .venv/bin/python <pkg>` (no pip module).
- Secrets in `.env` (gitignored): Webshare creds (`ualfuslo-fr-N` sticky FR),
  TWOCAPTCHA_API_KEY. Never commit secrets; redact in reports.
- Git: `main` → github.com/GeoManANZ/aisne-property-search.git (SSH). Commit +
  push after meaningful changes. GitHub CODE SEARCH IS UNRELIABLE on this repo —
  verify claims by grepping files, never by search index.
- Playwright browsers: `PLAYWRIGHT_BROWSERS_PATH=/opt/data/.playwright-browsers`.

## Source ownership (hard rules)
| Portal | Owner | Never |
|---|---|---|
| SeLoger | `seloger_api_sweep.py` (BFF API via Camoufox + FR sticky proxy) | HTML parsing / CAPTCHA / cascade on SeLoger |
| FNAIM, IAD, ParuVendu, lesiteimmo | `french_property_parsers.py ladder` | detail-page prices overriding card prices |
| Orpi + residual portals | `french_property_scraper.py` cascade (secondary) | expecting much: Cloudflare-blocked |

All sources now crawl BOTH immeuble + maison categories.

## Data-safety invariants (do not bypass)
1. All DB writes go through `bulk_upsert_validated` → `validate_listing()`
   (URL-shape rules per source, price/surface plausibility ranges).
2. Search-card/BFF price is authoritative; detail-scan prices are fallback only.
3. Price drops require corroboration: current DB price must equal the lower
   figure; implausible values excluded. Guards live in `config.PRICE_ALERT`.
4. Investment criteria live in `config.INVESTMENT` (min_surface_m2,
   price_min/max_eur, top_n) — amend there, not in report scripts.
5. Run `test_invariants.py` before ANY change to price extraction or validation:
   `.venv/bin/python test_invariants.py` (<2s, must exit 0).

## Common commands
```bash
cd /workspace/hermes1/projects/aisne-property-search/
.venv/bin/python seloger_api_sweep.py --max-pages 9 --sessions 8        # SeLoger
.venv/bin/python french_property_parsers.py ladder --max 2000 --delay 0.5   # all HTML sources (~18 min)
.venv/bin/python price_alert.py --json                                  # drops
.venv/bin/python recommendations_report.py --n 10                       # top deals
```
Sweep results land in `scans/<id>/consolidated_listings.json`; ingest happens
via the sweep script or manually through `bulk_upsert_validated`.

## Scheduled jobs (cron)
- **Aisne Fortnightly Sweep + LLM Report** (`0c51dae048fb`) — 1st & 15th 06:00 UTC.
  Sweep script has a freshness guard: skips crawling if last scan <20h old.
- **Aisne Price Drop Alert** (`61cf8b036698`) — daily 09:00 UTC, no_agent,
  silent unless genuine drops. Cron copy at `/opt/data/scripts/aisne_price_alert.py`
  — keep in sync after editing `price_alert.py`.
- **Aisne Weekly Recommendations** (`2a6b4b15dfb1`) — Mondays 08:00 UTC, Telegram MEDIA.

## Known state & quirks
- DB ≈3,300 rows, ~511 match investment criteria. Border spill-over into
  neighbouring depts is ACCEPTED (user decision — no postcode guard; Somme rows kept).
- Orpi: hard Cloudflare block, 1 row total — not a bug.
- SeLoger `/wl-cdp/` promoted cards structurally lack descriptions.
- Terrain-parcel surfaces (>5,000 m²) can pollute €/m² rankings — filter or cap
  when reporting.
- lesiteimmo dept search omits some live listings; the town sweep (270 town
  pages) recovers them — don't remove it.
- Cascade adaptive routing + cookie store ARE wired (route_engines /
  best_engine_for / get_cookies·save_cookies). Claims that they're missing are
  wrong — check file content first.

## Honesty rules
Never fabricate listings, prices, URLs, or scrape results. Verify live or report
the exact error/block. One decisive recommendation, not option menus.
