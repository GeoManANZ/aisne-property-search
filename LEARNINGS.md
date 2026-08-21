# Aisne Property Search — Project Learnings & Errors Log

**Project:** SeLoger/Orpi/FNAIM/IAD/ParuVendu scraping pipeline for Aisne (02) mixed-use properties
**Period:** 2026-08-18 → 2026-08-20
**Status:** Live — 150+ listings in DB, SeLoger pagination pipeline built

---

## 1. SeLoger Data Access — the three big lessons

### 1.1 Wrong pagination parameter cost a day (2026-08-20)
**Error:** Tested `?page=2`, `?pageNumber=2`, `/page-2`, `/2`, `?offset=32` — all ignored or 410.
**Root cause:** SeLoger's OLD `/list.htm` format used `?LISTING-LISTpg=N` (documented by Scrapfly + Medium). The NEW `/recherche/.../ad06fr2` SPA **ignores ALL URL pagination params** — it paginates purely via client-side state.
**Fix (verified live):** Click the real `à la page N` button in a browser. URL params are dead on the new format.
**Lesson:** Always verify the actual pagination mechanism per URL format — an article from 2022 documents the old format, the current SPA may differ.

### 1.2 SeLoger embeds data as JSON — browser rendering NOT needed for extraction
**Discovery:** `window["__UFRN_FETCHER__"]` + `window["__UFRN_STORE__"]` hold the FULL search state as `JSON.parse("...")` in the initial HTML.
- Cards: `data.classified-serp-init-data.pageProps.classifiedsData.<CARD_ID>`
- Real results: `pageProps.classifieds` (list, ~30) — **excludes** `suggestedClassifieds` (~4 repeating cards)
- State: `pageProps.page`, `pageProps.totalCount` (254-257 annonces)
**Implementation:** `parse_ufrn()` in `french_property_parsers.py` — returns same dict shape as `parse_seloger`, falls back to DOM parsing if JSON missing.
**Lesson:** For modern SPAs, look for embedded JSON state before fighting the DOM or clicking. 30 cards via JSON vs 17 via DOM.

### 1.3 The browser IS needed to trigger pagination (but not to extract)
**Error:** Tried pure-HTTP pagination with `LISTING-LISTpg` on the new URL — every page returned the same page-1 state.
**Fix:** Real browser (Camoufox) + real mouse/Playwright clicks on `à la page N` buttons. The SPA updates state client-side only.
**Lesson:** Extraction = HTTP + JSON.parse (cheap). Pagination = browser click (necessary). Split the two.

---

## 2. DataDome — prevention beats solving

### 2.1 2Captcha DataDome method quirks (verified 2026-08-20)
| Error | Cause | Fix |
|---|---|---|
| `ERROR_UNSUPPORTED_USERAGENT` | 2Captcha rejects Macintosh UAs | Normalize to Windows Chrome/124 UA |
| `ERROR_BAD_PROXY` | Proxy format wrong | Use `user:pass@host:port` |
| `ERROR_CAPTCHA_UNSOLVABLE` (instant) | HTTP proxytype rejected | Use `proxytype=socks5` |
| `ERROR_CAPTCHA_UNSOLVABLE` (130s full poll) | IP reputation — DataDome serves impossible variant | Cycle sessions; prevention instead |

**Cost lesson:** UNSOLVABLE failures are FREE (not charged). Only successful solves charge (~$0.003).

### 2.2 The real answer: browser + residential IP = no solve needed
**Discovery (the big one):** Camoufox (clean fingerprint) + Webshare French sticky IP (ualfuslo-fr-N) **passes DataDome with zero 2Captcha solve** — 30 real cards, no challenge page.
**But:** the IP must be CLEAN. French sticky sessions rotate IPs over time; some are DataDome-flagged.
**Fix:** Session-cycling — try fr-1..fr-8, use the first whose page-1 load has real content (no `geochallenge`/`captcha-delivery` in HTML).

### 2.3 DataDome research consensus
- Residential IP alone is NOT enough — DataDome behavioural-fingerprints even residential headless browsers.
- Winning combo: clean TLS/HTTP2 fingerprint + genuine browser env + human-like pacing + residential/mobile IP.
- Managed APIs (Scrapfly `asp=True`, ScrapeBadger) solve at scale but cost money.

---

## 3. Proxy Infrastructure — what actually works

### 3.1 WARP chain is fragile — direct backbone is reliable
**Error (recurring):** WARP→Webshare chain (`chain_socks_server.py` at 127.0.0.1:1081) flakes constantly — `NS_ERROR_UNKNOWN_HOST`, `Host unreachable`, connection refused under browser load.
**Fix:** The Webshare ROTATING plan's backbone IPs (`185.24.10.165:80`, `104.36.49.13:80`) are **directly reachable** from the VPS — no WARP needed. Camoufox points straight at `http://ualfuslo-fr-N:pass@185.24.10.165:80`.
**Lesson:** Rotating-plan backbone ≠ static per-IP list. The static IPs need WARP (VPS route TCP-blocked); the backbone doesn't.

### 3.2 Rotating plan session semantics (verified)
| Username | Behavior |
|---|---|
| `ualfuslo-rotate` | Per-request rotation |
| `ualfuslo-N` (e.g. `ualfuslo-1`) | Sticky session (same IP per request) — but rotates over MINUTES |
| `ualfuslo-fr` | Country-targeted (France), per-request rotation within country |
| `ualfuslo-fr-N` (e.g. `ualfuslo-fr-3`) | French + sticky ✅ the winner |

**Pitfall:** Sticky ≠ permanent. The IP CAN change over time (minutes). Always re-verify the egress IP matches the cookie/IP-bound state.

### 3.3 Chain server pitfalls (built, then abandoned)
- HTTP-CONNECT path through the backbone allocates DIFFERENT IPs than the raw-socket path for the same sticky session — cookie/IP mismatch.
- Dies under browser connection volume (light curl OK, browser fails).
- **Verdict:** Use direct backbone proxy in the browser. Don't build a local SOCKS forwarder unless truly necessary.

---

## 4. Browser Automation — the click that actually works

### 4.1 The REAL prize: SeLoger's BFF API (reverse-engineered 2026-08-20)
The SPA fetches SERP data from a clean JSON API — no HTML parsing needed:

```
POST https://www.seloger.com/serp-bff/search
Body: {
  "criteria": {
    "portals": ["SL"], "projectTypes": ["New_Build","Resale"],
    "distributionTypes": ["Buy"], "estateTypes": ["Building"],
    "location": {"placeIds": ["AD06FR2"]}
  },
  "paging": {"page": N, "size": 30, "order": "Default"}
}
Resp: {"totalCount": 257, "classifieds": [{"id": "..."} x30]}

GET https://www.seloger.com/classifiedList/<id1>,<id2>,...
Resp: [full card objects with price, location, livingArea, energy, url...]
```

- It's a POST (GET → 404 `Cannot GET`).
- `serp-bff/search` returns ONLY ids — the full cards come from `classifiedList/<ids>`.
- The browser earns the datadome cookie → then `page.evaluate(fetch(...))` rides it.
- Companion endpoints: `serp-bff/search/effect`, `search-mfe-bff/v1/count`, `search-mfe-bff/places/data`.

**classifiedList card schema (verified from raw response):**
```
{
  brand, id, status, hasAIEnrichment,
  metadata: {id, legacyId, creationDate, updateDate, ...},
  location: {address: {country, city, zipCode}, isAddressPublished},
  hardFacts: {title, price: {value, formatted, addition, ariaLabel}, facts: [{type, value, splitValue, label}]},
  tracking: {price: 156800, city: "Laon", ...},   ← clean int price!
  legacyTracking: {price, product_geolocation},
  provider: {intermediaryCard, contactCard, address},
  cardProvider: {title, subtitle},
  mainDescription: {headline, description, metadata},
  rawData: {price, providercity, surface: {main, plot}},
  energyClass, type, url, portal, tags, display, cps, gallery
}
```
**Field extraction (verified):** price → `tracking.price` (clean int) else `rawData.price` else `hardFacts.price.value`; surface → `rawData.surface.main` else `hardFacts.facts[]` (type contains space/surface/area); location → `location.address.city` + zipCode else `tracking.city`; DPE → `energyClass`.

### 4.2 Pagination button click
**Error (hours):** `page.mouse.click(x, y)` at coordinates FAILS (overlay hit-testing) even after `scrollIntoView({block:'center'})`.
**Fix:** **Playwright `locator.click()`** — does proper actionability checks (auto-scroll, hit-target, real mouse events). Verified: page advances instantly.
**Then:** pages 3+ fail with locator.click (element "not stable" during SPA re-render, 8s timeout).
**Final fix:** **JS native `b.click()`** — `scrollIntoView({block:'center'})` + `b.click()` fires React's synthetic handler reliably. Verified: page 3 via JS click.
**Also:** the pagination nav sits ~15,000px down the page (below suggested cards + market insights) — must auto-scroll.

### 4.3 Sticky session IP rotation breaks sessions mid-sweep
**Error:** Page 1 loads 30 cards, then a later page's click fails because the IP rotated and the new IP is challenged.
**Fix:** Session-cycling at START + retry-on-stuck clicks mid-sweep. (Known remaining issue: page 3+ occasionally stuck — nav re-render timing.)

### 4.4 Camoufox notes
- `geoip=True` recommended when using a proxy (LeakWarning) — but `geoip=False` works fine.
- Browser context cookies ≠ page cookies — use `context.add_cookies()`.
- Direct backbone proxy via `http://user:pass@host:port` in `proxy.server` works; credentials NOT embedded in URL for Playwright/Camoufox (timeout) — **actually verified: direct URL with creds DOES work for Camoufox**.

---

## 5. Cost Accounting (2026-08-20)

| Item | Cost |
|---|---|
| 2Captcha balance start | $5.00 |
| Balance end | ~$4.94 |
| Actual spend | ~$0.06 (successful solves only) |
| UNSOLVABLE failures | $0 (free) |

**Lesson:** CAPTCHA solving is nearly free when failures don't charge — but it's still the wrong tool when a browser + residential IP avoids the challenge entirely.

---

## 6. Pipeline Architecture (final, correct)

```
SeLoger SERP URL
    → Camoufox browser (direct backbone proxy, fr-N session)
    → verify page 1 loads REAL content (no challenge); cycle session if flagged
    → for page 2..9: Playwright click "à la page N" (retry on stuck)
    → save page HTML
    → parse_ufrn() → __UFRN_FETCHER__ JSON → classifieds (exclude suggested)
    → upsert listings.db (bulk_upsert, dedup by URL)
```

**Zero 2Captcha spend.** One browser session. ~30 listings/page × 9 pages ≈ 250 unique.

---

## 7. Still Open / Known Issues

1. **Page 3+ click flakiness** — after page 2, clicking page 3 sometimes doesn't advance (nav re-render). Retry logic added; needs one more hardening pass (e.g. longer post-click settle, or click `page suivante` instead of `à la page N`). *(De-prioritised: the BFF API path in §6 replaces browser click-through entirely.)*
2. **Session IP rotation mid-sweep** — if the winning session's IP rotates to a flagged one mid-run, pages fail. Session-cycling at start handles most cases.
3. **Zilek (Turnstile)** — never attempted with 2Captcha.
4. ~~Property report .MD Telegram delivery~~ — **DONE**: `recommendations_report.py` generates top-N Markdown; weekly cron (`Aisne Weekly Recommendations Report`, Mon 08:00 UTC) delivers via Telegram MEDIA. Price-drop alerts already delivered daily 09:00 UTC via `aisne_price_alert.py` cron (surface + €/m² included).
5. ~~DB historical malformed prices~~ — **DONE**: validation layer (bulk_upsert_validated) rejects out-of-range prices/surfaces at ingest; all 395 current rows pass.

---

## 8. Improvement Opportunities

1. ~~Full JSON schema extraction~~ — **DONE**: features (245/256), tags+photos (256/256) captured from classifiedList; schema migrated with `features`/`tags` columns.
2. **Scheduled sweeps** — cron the pagination sweep daily; DB has `status`/`last_check` columns for bookkeeping.
3. **Price-drop alerts** — `price_drops(days)` windowed query exists; wire into a daily notification.
4. **Managed API evaluation** — Scrapfly/ScrapeBadger as a paid fallback if free browser path degrades.
5. **Mobile-IP rotation** — DataDome research says mobile IPs score even higher trust; Webshare plan may offer mobile exits.
6. ~~Detail-page enrichment~~ — **DONE** (ParuVendu): descriptions enriched 336→645 chars avg via scan_detail_page; JS noise cleaned.

---

## 9. Price-Drop Alerts — false-positive prevention (learned 2026-08-21)

The first price-alert run reported 4 ParuVendu "drops" that were **all false**. Root cause chain:
1. **Detail-page price parser is unreliable** — `scan_detail_page` grabbed hidden/embedded numbers (e.g. 58,248 €) instead of the visible price (808,000 €) on ParuVendu detail pages.
2. **The detail-scan OVERWROTE good prices** — the ladder merge used `detail.get("price_eur") or item.get("price_eur")`, letting the bad detail price override the correct search-card price.
3. **The alert trusted the polluted history** — it saw 808,000 → 58,248 and reported a 93% "drop."

**Fixes (all applied):**
- **Merge precedence** — search-card price now takes priority; detail price only used as fallback.
- **Alert corroboration guard** — a drop is only reported if the CURRENT `listings.price_eur` equals the lower (new) price. Transient/artifact values never match, so they're discarded.
- **Live verification** — manually confirmed real prices (808,000, 460,000, etc.) via curl and corrected the DB + removed bogus `price_history` rows.

**Rule:** NEVER trust a price change from a single scrape source. The structured search-card/API price is authoritative; detail-page parses are suspect; and an alert must be corroborated by the listing's current price before it's reported.

---

## 10. Cookie reuse + fingerprint consistency (learned 2026-08-21)

The most expensive step in the SeLoger path is NOT the fetch — it's the
**browser launch + DataDome challenge** that earns the `datadome` cookie.
Once earned, that cookie is honoured for hours on the SAME residential IP.

**Integration (seloger_api_sweep.py):**
- `cookie_store.py` persists the earned cookies per sticky session key
  (`ualfuslo-fr-<N>` — the sticky session always resolves to the same IP,
  so the session id IS the cookie key).
- Before launching Camoufox: `get_cookies(session_key, "www.seloger.com")`
  → `ctx.add_cookies(...)` — skips the challenge entirely on warm runs.
- After a successful sweep: extract `ctx.cookies()` filtered to seloger.com
  → `save_cookies(...)` for the next run.
- **Fingerprint consistency:** one `build_fingerprint()` per session threads
  the SAME UA / locale / timezone / viewport into the Camoufox context.
  `page.evaluate(fetch...)` inherits the context fingerprint automatically.
  DataDome re-challenges on mixed fingerprints — never mix UA sources.

**Rule:** earn the cookie once per sticky IP, persist it, reuse it. Treat
2Captcha/DataDome solving as the LAST resort, not the default.

## 11. Reproducibility (2026-08-21)

- `requirements.txt` pinned from working venv (camoufox 0.5.5, playwright
  1.60, lxml 6.1.1, requests, PySocks, playwright-stealth).
- `cron.example` — daily sweep/ladder/alert schedules (cron + systemd timer).
- `.env` is the single credential source; config.py FAILS LOUDLY (no silent
  fallback to another project's proxy_config.py anymore).
