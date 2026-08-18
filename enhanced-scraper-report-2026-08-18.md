# Aisne (02) — Soissons–Laon Corridor: Enhanced Scraper Report

**Date:** 2026-08-18 · **Engine:** direct → WARP → Lightpanda → Playwright stealth
**Sources:** FNAIM (25) + IAD (30) + ParuVendu (15) = 70 listings scanned live
**Files:**
- `french-property-scraper.py` — 4-engine cascade scanner (heavily commented)
- `french-property-parsers.py` — FNAIM/IAD/ParuVendu search + detail parsers
- `.venv/` — Python venv with requests, PySocks, playwright, playwright-stealth

## Engine capability matrix (tested live today)

| Portal | direct | warp | lightpanda | stealth | Verdict |
|---|---|---|---|---|---|
| FNAIM | ✅ | — | — | — | fully accessible |
| iad | ✅ | — | — | — | fully accessible |
| ParuVendu | ✅ | — | — | — | fully accessible |
| Orpi | ❌ 403 | ❌ 403 | ❌ block | ❌ hard block | **needs residential IP** |
| SeLoger | ❌ | ❌ | — | ⚠️ 200 but DataDome CAPTCHA | **needs CAPTCHA solve** |
| Logic-Immo | ❌ | ❌ | — | ⚠️ 200 but DataDome | **needs CAPTCHA solve** |
| Superimmo | ❌ 503 | ❌ | — | ⚠️ click-CAPTCHA | **needs human/click** |
| Zilek | ❌ | ❌ | ❌ challenge | ❌ "Un instant…" loop | **needs real browser** |

Key insight: **stealth defeats WAF fingerprinting** (403 → 200 OK) but the
top portals (SeLoger, Logic-Immo) then layer **DataDome interactive
CAPTCHAs** which require either residential proxies, CAPTCHA-solving
services, or human clicks. Orpi uses a **hard IP-block rule** that no
fingerprint trick bypasses — only a clean residential IP works.

## TARGET ZONE: ≤€220k, ≥150 m² — 26 live listings (with links)

| # | Price | m² | DPE | €/m² | Source | Link |
|---|-------|-----|-----|-------|--------|------|
| 1 | 5,200 € | 250 | E | 21 | paruvendu | https://www.paruvendu.fr/immobilier/vente/immeuble/1288822442A1KIVHIM000 |
| 2 | 33,900 € | 664 | ? | 51 | fnaim | https://www.fnaim.fr/annonce-immobiliere/52588214/17-acheter-immeuble-aisne-2.htm |
| 3 | 58,248 € | 451 | D | 129 | paruvendu | https://www.paruvendu.fr/immobilier/vente/immeuble/1290134701A1KIVHIM000 |
| 4 | 79,990 € | 217 | ? | 369 | fnaim | https://www.fnaim.fr/annonce-immobiliere/52827207/17-acheter-immeuble-origny-en-thierache-02550.htm |
| 5 | 99,900 € | 170 | ? | 588 | fnaim | https://www.fnaim.fr/annonce-immobiliere/52810327/17-acheter-immeuble-aisne-2.htm |
| 6 | 104,500 € | 284 | ? | 368 | iad | https://www.iadfrance.fr/annonce/immeuble-vente-le-nouvion-en-thierache-284m2/r1881103 |
| 7 | 106,000 € | 230 | G | 461 | fnaim | https://www.fnaim.fr/annonce-immobiliere/52954370/17-acheter-immeuble-chauny-02300.htm |
| 8 | 109,990 € | 250 | C | 440 | fnaim | https://www.fnaim.fr/annonce-immobiliere/52957996/17-acheter-immeuble-hirson-02500.htm |
| 9 | 116,590 € | 250 | D | 466 | fnaim | https://www.fnaim.fr/annonce-immobiliere/53121104/17-acheter-immeuble-st-michel-02830.htm |
| 10 | 119,990 € | 430 | ? | 279 | fnaim | https://www.fnaim.fr/annonce-immobiliere/52722152/17-acheter-immeuble-hirson-02500.htm |
| 11 | 123,000 € | 260 | ? | 473 | fnaim | https://www.fnaim.fr/annonce-immobiliere/51387475/17-acheter-immeuble-le-nouvion-en-thierache-02170.htm |
| 12 | 137,000 € | 225 | ? | 609 | iad | https://www.iadfrance.fr/annonce/immeuble-vente-saint-quentin-225m2/r1911358 |
| 13 | 144,000 € | 190 | ? | 758 | iad | https://www.iadfrance.fr/annonce/immeuble-vente-coincy-190m2/r2016263 |
| 14 | 151,000 € | 274 | E | 551 | paruvendu | https://www.paruvendu.fr/immobilier/vente/immeuble/1293850256A1KIVHIM000 |
| 15 | 153,000 € | 150 | ? | 1020 | iad | https://www.iadfrance.fr/annonce/immeuble-vente-saint-quentin-150m2/r2062796 |
| 16 | 159,900 € | 215 | ? | 744 | fnaim | https://www.fnaim.fr/annonce-immobiliere/52879879/17-acheter-immeuble-brancourt-en-laonnois-02320.htm |
| 17 | 167,000 € | 385 | ? | 434 | fnaim | https://www.fnaim.fr/annonce-immobiliere/52876715/17-acheter-immeuble-le-nouvion-en-thierache-02170.htm |
| 18 | 168,000 € | 207 | F | 812 | fnaim | https://www.fnaim.fr/annonce-immobiliere/53018635/17-acheter-immeuble-st-michel-02830.htm |
| 19 | 169,000 € | 386 | ? | 438 | iad | https://www.iadfrance.fr/annonce/immeuble-vente-hirson-386m2/r1869298 |
| 20 | 178,500 € | 350 | C | 510 | fnaim | https://www.fnaim.fr/annonce-immobiliere/53124775/17-acheter-immeuble-st-michel-02830.htm |
| 21 | 183,000 € | 220 | ? | 832 | iad | https://www.iadfrance.fr/annonce/immeuble-vente-la-ville-aux-bois-les-pontavert-220m2/r1873097 |
| 22 | 184,000 € | 180 | ? | 1022 | iad | https://www.iadfrance.fr/annonce/immeuble-vente-saint-quentin-180m2/r2019587 |
| 23 | 189,000 € | 303 | ? | 624 | fnaim | https://www.fnaim.fr/annonce-immobiliere/51693072/17-acheter-immeuble-amifontaine-02190.htm |
| 24 | 199,000 € | 230 | ? | 865 | iad | https://www.iadfrance.fr/annonce/immeuble-vente-chateau-thierry-230m2/r1958569 |
| 25 | 209,900 € | 184 | ? | 1141 | fnaim | https://www.fnaim.fr/annonce-immobiliere/53059141/17-acheter-immeuble-laon-02000.htm |
| 26 | 219,000 € | 230 | ? | 952 | iad | https://www.iadfrance.fr/annonce/immeuble-vente-laon-230m2/r2041287 |

## Top 5 recommendations

1. **Origny-en-Thiérache — 217 m² · 79,990 € · 369 €/m²** — ancien bar «Relais du Cheval Blanc», 5 chbr à l'étage, PAC installée, à rénover entièrement. Best €/m² in the corridor.
2. **Brancourt-en-Laonnois — 215 m² · 159,900 € · 744 €/m²** — immeuble invest, 3 logements loués, rentabilité nette ~9%, axe Laon–Soissons. L'Atelier Immo.
3. **Le Nouvion-en-Thierache — 284 m² · 104,500 € · 368 €/m²** — large immeuble, cheapest €/m² above 100m².
4. **Soissons — 274 m² · 151,000 € · 551 €/m²** — local commercial RDC + appt 4 chbr + grenier divisible; terrain clos 1,618 m². Closest twin to the €84,700 reference.
5. **Saint-Michel — 350 m² · 178,500 € · 510 €/m²** — immeuble DPE C, 510 €/m².

## Notes on suspicious ultra-low listings

- 5,200 € / 250 m² (ParuVendu) — likely terrain/parking, verify
- 33,900 € / 664 m² (FNAIM) — likely terrain/lot, verify
- 58,248 € / 451 m² (ParuVendu) — verify nature (could be terrain)

## Commands

```bash
# Full sweep of the 3 accessible portals (search + detail pages)
cd /workspace/hermes1/projects/aisne-property-search/
.venv/bin/python french-property-parsers.py ladder --detail-scan all

# Single URL through the full cascade
.venv/bin/python french-property-scraper.py --engine cascade --url "https://..."
# Stealth only, no WARP:
.venv/bin/python french-property-scraper.py --engine stealth --stealth-no-warp --url "https://..."
```
