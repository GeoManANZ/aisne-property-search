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
   max_surface_m2, price_min/max_eur, top_n) — amend there, not in report scripts.
   max_surface_m2=1500 kills lesiteimmo land-parcel mis-parses (they label
   terrain/forest as "maison" with land area in "surface habitable").
5. Run `test_invariants.py` before ANY change to price extraction or validation:
   `.venv/bin/python test_invariants.py` (<2s, must exit 0).
6. **Reports may only show LIVE listings.** `recommendations_report.py` filters
   `COALESCE(status,'active')='active'`; `check_liveness.py` is what sets
   `status='gone'`. Without the liveness pass, ads that sold months ago keep
   resurfacing (the 2026-09-10 bug: 3,568 rows all 'active' forever, and users
   clicked through to "Annonce supprimée").
7. **A sale is a data point, not a lost lead.** When a listing leaves the market
   it goes to `disposals` (days_on_market, €/m², town, evidence string) and
   `listing_events` — never silently dropped.

## Liveness & disposition (`check_liveness.py`)
- Classifies each listing: `live` / `gone:sold` (`vendu`, `sous compromis`,
  `sous offre`) / `gone:withdrawn` (`annonce supprimée`, `n'est plus disponible`)
  / `gone:dead_url` (404/410) / `blocked` (403/429 bot wall).
- `blocked` is NEVER treated as gone — a bot wall is not evidence either way.
- Regex trap: match `\bvendu\b`, not bare `vendu`, or agency blurbs
  ("honoraires vendeur") manufacture sales that never happened.
- Baseline (2026-09-10, 543 reportable listings): 368 live, 104 blocked,
  36 dead_url, 35 sold → 13.4% of stock was already dead.
- **SeLoger cannot be verified by direct fetch** — it 403s, so its rows stay
  `blocked` with possibly dead links. See the SeLoger verification section below.
- Run: `.venv/bin/python check_liveness.py --all-criteria --quiet`
  (direct egress, zero Webshare cost). Wired into the fortnightly sweep.

## Sales / sold database (`ingest_dvf.py`, `sales_report.py`)
- **`dvf_sales`** = official Etalab DVF notarial sales, geolocated, free, direct
  egress: `files.data.gouv.fr/geo-dvf/latest/csv/<YEAR>/departements/<DEPT>.csv.gz`
  (~0.8 MB/dept/year; years 2021-2025). Dept 02 = 150,470 raw rows.
- **`dvf_buildings`** (one row per mutation/sale) and **`dvf_sales_clean`**
  (building sales with €/m²) are MATERIALIZED TABLES rebuilt on ingest — not
  views. Rebuild: `.venv/bin/python ingest_dvf.py --depts 02` (~4s).
- **DVF double-count trap (this produced a wrong published number once):** one
  mutation = many rows, one per local per `nature_culture`, each with the SAME
  `valeur_fonciere` but a DIFFERENT `surface_terrain`. A `DISTINCT` over all
  columns fails to collapse them → `SUM(surface_reelle_bati)` doubles (282 m²
  instead of 141 m²) and €/m² is understated. Dedupe building rows WITHOUT
  surface_terrain; aggregate land separately. Anchor: mutation `2024-12830`
  must yield 141 m², 1 local, €273,000.
- Interrogate: `.venv/bin/python sales_report.py --overview | --communes 12 |
  --trend | --eur-m2 "Hirson" | --recent "Soissons" 5 | --vs-asking | --disposals`
- `--vs-asking` compares OUR asking €/m² against ACHIEVED sold €/m² per commune.
  Caveat: our stock is mostly large (≥150 m²) buildings needing work, so it sits
  legitimately below the all-sales average. Rank towns against each other, don't
  read the absolute gap as pure discount.
- Town joins are normalised in Python (`norm_town`: accent-strip, drop postcode
  and trailing dept code). Joining with LIKE on the first word produced nonsense
  ("Saint" from "Saint-Quentin", "La" from "La Fère").

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
- DB ≈3,300+ rows (ParuVendu 309 after dept-wide fix), ~424+ distinct candidates
  match investment criteria after dup collapse. Border spill-over into
  neighbouring depts is ACCEPTED (user decision — no postcode guard; Somme rows
  kept).
- Orpi: hard Cloudflare block, 1 row total — not a bug.
- SeLoger `/wl-cdp/` promoted cards structurally lack descriptions.
- Terrain-parcel surfaces pollute €/m² rankings — lesiteimmo labels land as
  "maison"; `config.INVESTMENT.max_surface_m2=1500` caps them. Real buildings
  are all ≤1,000 m²; land/forest/étangs rows are 4,000–96,000 m².
- ParuVendu paginates with `?p=N` NOT `?page=N` — pagination param lives in
  `PAGINATION_PARAM`. A 404/410 on p=N+1 is END-OF-PAGINATION (fetch_page
  returns err='end'), NOT fatal — before 2026-08-26 the ladder only ever got
  page 1 (~30 of 279 annonces the portal actually serves; its "1,530 annonces"
  SEO count is inflated — pagination stops at p=5).
- IAD URL slugs may carry a pieces-count prefix (`maison-vente-1-piece-hirson-400m2`)
  — parser skips it; IAD locations have no postcode (dedup handles via
  postcode-compatible matching).
- lesiteimmo dept search omits some live listings; the town sweep (270 town
  pages) recovers them — don't remove it.
- Cascade adaptive routing + cookie store ARE wired (route_engines /
  best_engine_for / get_cookies·save_cookies). Claims that they're missing are
  wrong — check file content first.

## Honesty rules
Never fabricate listings, prices, URLs, or scrape results. Verify live or report
the exact error/block. One decisive recommendation, not option menus.
