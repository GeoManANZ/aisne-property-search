# SeLoger Scraping — How to Do It Better and Correctly

**Date:** 2026-08-20 · **Research:** Scrapfly guide, Medium pagination article, live HTML reverse-engineering, live proxy testing

---

## TL;DR — the three mistakes that wasted the day

1. **Wrong pagination param.** SeLoger paginates with `?LISTING-LISTpg=N` (documented by Scrapfly + Medium). I tested `?page=2`, `?pageNumber=2`, `/page-2`, `?offset=32` — all wrong. Never tried `LISTING-LISTpg`.
2. **Browser click-through was unnecessary.** SeLoger embeds ALL card data as JSON in the initial HTML (`window["__UFRN_FETCHER__"]` + `window["__UFRN_STORE__"]`), keyed by card ID, with `pageProps` containing `page`, `totalCount`, `searchModel`, `classifieds`. The SPA "click" battle was fighting the wrong layer — the data is already server-rendered into JSON.
3. **DataDome solve reliability was the real bottleneck — and it's mostly a 2Captcha service issue, not our code.** Fixes found: UA normalization (2Captcha rejects Macintosh UAs with `ERROR_UNSUPPORTED_USERAGENT`), session cycling (some French IPs flagged), and it's better to **avoid solving at all** by preventing the challenge.

---

## The correct architecture (what to build instead)

### 1. Pure-HTTP pagination — no browser
```
for page in 1..9:
    url = f"https://www.seloger.com/recherche/achat/immeuble/hauts-de-france/aisne-02/ad06fr2?LISTING-LISTpg={page}"
    html = fetch_with_cookie(url, session=fr-N)     # RotatingWebshareSession
    data = parse_ufrn(html)                          # JSON.parse the __UFRN_* blobs
    cards = data['classified-serp-init-data']['pageProps']
    save(cards['classifiedsData'], cards['totalCount'], cards['page'])
```
- **One DataDome solve** → one sticky French IP (`ualfuslo-fr-3`) → all 9 pages via plain HTTP.
- No Camoufox, no clicking, no scroll-waiting, no chain server.
- 9 requests total; each returns the full JSON state.

### 2. Structured JSON extraction (not DOM parsing)
The card data lives at:
```
window["__UFRN_FETCHER__"] → JSON.parse → data.classified-serp-init-data.pageProps.classifiedsData.<CARD_ID>
```
Each card has: `id`, `location`, `hardFacts` (price/surface), `energyClass`, `url`, `mainDescription`, `provider`, `type`, `tags`.
This is **schema-stable and much richer** than the DOM cards — includes suggested listings, enriched data, market insights.

Pagination state: `pageProps.page`, `pageProps.totalCount` (254 annonces confirmed), `pageProps.searchModel`.

### 3. Prevention over solving (the actual best practice)
DataDome research consensus (Olostep, ScrapeBadger, Scrapfly):
- **Residential IP alone is NOT enough** — DataDome behavioural-fingerprints even residential headless browsers.
- The winning combination: **clean TLS/HTTP2 fingerprint + genuine browser env + human-like pacing + warm-up navigation + residential/mobile IP**.
- Managed scraping APIs (Scrapfly `asp=True` + `country=fr`, ScrapeBadger) solve this with one call — they rotate residential IPs, do JS rendering, and manage cookies. Costs money but is the *reliable* path.
- **Use a real browser session that solves once and reuses the cookie across requests** — the cookie is IP-bound, so pin ONE sticky session for the whole sweep.

---

## What we got right (already in place)

- ✅ **Rotating residential plan** — `ualfuslo-fr-N` sticky French sessions (proven: same IP per session, country targeting works)
- ✅ **UA normalization fix** — 2Captcha's DataDome method needs Windows Chrome UA (Macintosh → `ERROR_UNSUPPORTED_USERAGENT`)
- ✅ **SOCKS5 proxytype for 2Captcha** — HTTP proxytype returns UNSOLVABLE; SOCKS5 solves (verified live fr-3 success)
- ✅ **Cookie reuse** — datadome cookie saved keyed by (session, domain); avoids re-solving
- ✅ **`RotatingWebshareSession`** — direct backbone, no WARP flakiness
- ✅ **Session-cycling sweeper** — tries fr-1..fr-12 until one solves

## What to change

| # | Change | Effort |
|---|--------|--------|
| 1 | Add `LISTING-LISTpg=N` pagination to the sweep (pure HTTP, no browser) | Small |
| 2 | Add `parse_ufrn()` — extract `__UFRN_FETCHER__`/`__UFRN_STORE__` JSON | Small |
| 3 | Replace DOM `parse_seloger` with structured JSON extraction | Small-Med |
| 4 | Pin one winning session for the whole sweep; verify egress IP matches cookie IP | Small |
| 5 | Consider managed API (Scrapfly/ScrapeBadger) for production reliability | Decision |

## Verification steps for the fix
```bash
# 1. Confirm LISTING-LISTpg changes pages (needs a solved cookie)
curl --proxy "http://ualfuslo-fr-3:...@185.24.10.165:80" \
  "https://www.seloger.com/recherche/achat/immeuble/hauts-de-france/aisne-02/ad06fr2?LISTING-LISTpg=2"

# 2. Confirm __UFRN_FETCHER__ JSON parses
python -c "import re,json; h=open('page.html').read(); \
m=re.search(r'window\[\"__UFRN_FETCHER__\"\]=JSON.parse\(\"(.+?)\"\);',h,re.DOTALL); \
d=json.loads(m.group(1).encode().decode('unicode_escape')); \
print(len(d['data']['classified-serp-init-data']['pageProps']['classifiedsData']))"

# 3. Compare card URLs between page 1 and page 2 — must differ
```

## Cost summary of today's experiment
- **2Captcha balance: $4.94** (from $5.00). All UNSOLVABLE failures were FREE. Only successful solves charged (~$0.003 each).
- The rotating plan costs ~$0.11/GB at this rate (bonus: the user's rotating purchase is the right long-term tool).

## The honest bottom line
The pagination problem was **solved in research, not in code**. The browser click-through was the wrong tool; the correct fix is `LISTING-LISTpg` + `__UFRN_*` JSON parsing over the sticky French session — an afternoon of implementation, no CAPTCHA-heavy fighting. The DataDome solve is needed once per session, not per page.
