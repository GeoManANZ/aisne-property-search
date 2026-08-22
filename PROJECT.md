# PROJECT.md — Aisne Property Search (operational guide for Hermes)

**Purpose:** mixed-use / immeuble de rapport property pipeline for the Soissons–Laon corridor. Read this first in any new session; verify against live files before acting.

- **Path:** `/workspace/hermes1/projects/aisne-property-search/`
- **Git:** `main`, remote `git@github.com:GeoManANZ/aisne-property-search.git` — always push after meaningful commits
- **Python:** `.venv/bin/python` only
- **Secrets:** `.env` (Webshare creds required; config.py fails loudly without them)

## Run commands

```bash
cd /workspace/hermes1/projects/aisne-property-search/
.venv/bin/python seloger_api_sweep.py --max-pages 9 --sessions 8   # SeLoger (primary)
.venv/bin/python french_property_parsers.py ladder --sources fnaim iad paruvendu --max 200 --delay 1
.venv/bin/python price_alert.py --json                              # genuine drops only
.venv/bin/python recommendations_report.py --n 20 --out report.md   # top-N by €/m²
```

## Architecture (what owns what)

| Concern | File |
|---|---|
| SeLoger (BFF API + Camoufox + FR sticky proxy) | `seloger_api_sweep.py` — THE SeLoger path; never HTML-parse or CAPTCHA-solve SeLoger |
| FNAIM / IAD / ParuVendu parsers + ladder | `french_property_parsers.py` |
| SQLite store + validation + price history | `listings_db.py` (`listings.db` is gitignored) |
| Price-drop detection (corroboration-guarded) | `price_alert.py`; cron copy at `/opt/data/scripts/aisne_price_alert.py` |
| Top-N report generator | `recommendations_report.py` |
| Config: secrets, proxies, fingerprint, DEPARTMENTS | `config.py` |
| Cookie persistence per sticky IP | `cookie_store.py` |
| Engine metrics + adaptive cascade routing | `engine_metrics.py` |
| Generic multi-engine cascade (SECONDARY: Orpi/detail enrichment) | `french_property_scraper.py` (lazy-imported) |

Legacy experiments live in `archive/` — do NOT resurrect.

## Invariants (never violate)

1. **Search-card price is authoritative.** Detail-page price parses are fallback-only — they once overwrote a real €808,000 price with a hidden €58,248.
2. **Price alerts require corroboration** — current DB price must equal the lower figure; plausible range €10k–€50M.
3. **All writes go through `bulk_upsert_validated()`** (URL-shape, price/surface guards at ingest).
4. **Never fabricate data.** Verify live before asserting; report blocked as blocked.
5. **Cookie reuse:** datadome cookie keyed by sticky session id (`ualfuslo-fr-N`); inject before Camoufox launch, save all context cookies after success.
6. One fingerprint per session via `build_fingerprint()`; `geoip=True` with proxy.

## Search area & filters

- Department defined in `config.py → DEPARTMENTS` (place_id + slug). Currently Aisne only (`AD06FR2`). Extend by adding an entry (ladder SOURCE_URLS need per-department URLs too).
- Investment filters (report/alert layer): ≥150 m², €10k–€220k, ranked €/m² ascending — constants at top of `recommendations_report.py`.
- Known quirk: SeLoger BFF leaks ~9 near-border Somme listings into Aisne results — user accepted, leave them.

## Automation (Hermes crons)

- `Aisne Price Drop Alert` (61cf8b036698) daily 09:00 UTC, no_agent, silent when no drops.
- `Aisne Weekly Recommendations Report` (2a6b4b15dfb1) Mon 08:00 UTC → Telegram MEDIA.

## Known data-quality state (2026-08-22)

397 rows (seloger ~331, iad 31, fnaim ~23, paruvendu 11, orpi 1), all pass validation. Gaps that are structural, not bugs: `/wl-cdp/` promoted cards lack descriptions; a handful of thin cards from small portals lack surface/location.

## Debugging quick refs

- Full lessons: `LEARNINGS.md` (esp. §9 false-drop prevention, §10 cookie/fingerprint, §11 reproducibility)
- Agent onboarding prompt: `AI-ASSISTANT-PROMPT.md`
- Live-test logs: `logs/`
- Engine metrics summary: `.venv/bin/python -c "from engine_metrics import summary; print(summary())"`
