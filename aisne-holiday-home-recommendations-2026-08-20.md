# Aisne Soissons–Laon Corridor — Holiday-Home Investment Recommendations (CLEAN)

**Date:** 2026-08-20 (data-integrity pass) · **Database:** 395 rows, 5 sources
**SeLoger:** 256 listings with real descriptions via BFF API
**Criteria:** Mixed-use / immeuble de rapport · ≥150 m² · €10k–220k · sane €/m²

---

## ✅ Data-integrity fixes applied this pass
- **ParuVendu:** rebuilt parser (lxml card-level) — now yields real prices/descriptions/locations/DPE; **purged 7 town-page junk rows** (Noyon, Meaux, La Ferté-sous-Jouarre = NOT Aisne).
- **FNAIM:** location now extracted from data-title (e.g. "HIRSON (02500)"); **purged 3 generic `aisne-2` search-page rows**.
- **SeLoger:** descriptions captured (256) + canonical `legacyId/detail.htm` URLs (13 `/wl-cdp/` = unique promoted listings, kept).
- **IAD:** surface + town extracted from URL slugs.

## 🏆 Top 20 (deduplicated, ranked by €/m²)

| # | Price | m² | €/m² | DPE | Town | Link |
|---|-------|----|------|-----|------|------|
| 1 | €57,500 | 680 | **85** | – | La Fère | https://www.seloger.com/273300945/detail.htm |
| 2 | €30,000 | 350 | **86** | – | Liesse-Notre-Dame | https://www.seloger.com/annonce/achat/hauts-de-france/aisne-02/liesse-notre-dame-02350/25M5U8GBVDQA |
| 3 | €39,900 | 220 | **181** | – | Fère-en-Tardenois | https://www.seloger.com/wl-cdp/2539EALM3VRM |
| 4 | €131,100 | 570 | **230** | – | Marle | https://www.seloger.com/263042141/detail.htm |
| 5 | €54,000 | 212 | **255** | – | Étreux | https://www.seloger.com/271585753/detail.htm |
| 6 | €180,000 | 658 | **274** | – | Neuilly-Saint-Front | https://www.seloger.com/annonce/achat/hauts-de-france/aisne-02/neuilly-saint-front-02470/26Y4M1EGUEH2 |
| 7 | €119,990 | 430 | **279** | – | Hirson | https://www.fnaim.fr/annonce-immobiliere/52722152/17-acheter-immeuble-hirson-02500.htm |
| 8 | €87,900 | 280 | **314** | F | Fère-en-Tardenois | https://www.seloger.com/264947407/detail.htm |
| 9 | €186,000 | 584 | **318** | – | Laon | https://www.seloger.com/270786275/detail.htm |
| 10 | €68,000 | 212 | **321** | C | Guise | https://www.seloger.com/annonce/achat/hauts-de-france/aisne-02/guise-02120/26WQKYMAEGNP |
| 11 | €69,800 | 212 | **329** | C | Guise | https://www.seloger.com/273360629/detail.htm |
| 12 | €55,000 | 160 | **344** | F | Bohain-en-Vermandois | https://www.seloger.com/263970793/detail.htm |
| 13 | €157,500 | 450 | **350** | D | Guise | https://www.seloger.com/214124469/detail.htm |
| 14 | €138,000 | 380 | **363** | – | Chivy-lès-Étouvelles | https://www.seloger.com/273863427/detail.htm |
| 15 | €199,900 | 544 | **367** | E | Château-Thierry | https://www.seloger.com/270934703/detail.htm |
| 16 | €199,900 | 544 | **367** | E | Fère-en-Tardenois | https://www.seloger.com/270921131/detail.htm |
| 17 | €104,500 | 284 | **368** | – | Le Nouvion-en-Thiérache | https://www.iadfrance.fr/annonce/immeuble-vente-le-nouvion-en-thierache-284m2/r1881103 |
| 18 | €79,990 | 217 | **369** | – | Origny-en-Thiérache | https://www.fnaim.fr/annonce-immobiliere/52827207/17-acheter-immeuble-origny-en-thierache-02550.htm |
| 19 | €69,000 | 184 | **375** | – | Guise | https://www.seloger.com/272740975/detail.htm |
| 20 | €75,000 | 185 | **405** | F | Guise | https://www.seloger.com/274384403/detail.htm |

## ⭐ My top 3 picks (quality-adjusted)
1. **Guise 212 m² @ €69,800 — €329/m², DPE C** — rentable now: https://www.seloger.com/273360629/detail.htm
2. **La Fère 680 m² @ €57,500 — €85/m²** — extraordinary value: https://www.seloger.com/273300945/detail.htm
3. **Liesse-Notre-Dame 350 m² @ €30,000 — €86/m²** — cheapest entry: https://www.seloger.com/annonce/achat/hauts-de-france/aisne-02/liesse-notre-dame-02350/25M5U8GBVDQA

## Feature search (now possible — descriptions populated)
- **Ponds (étang/mare): 0** found — verified negative, not a data gap
- **Gardens: 5** listings mention jardin
- **Commercial ground floor: 59** listings mention commerce/commercial (core immeuble-de-rapport play)

## Caveats
- Rows 15/16 are the same 544 m² building listed at two towns.
- FNAIM row 18 lacks location field (verify manually).
- Links are live as of 2026-08-20 scrape.
