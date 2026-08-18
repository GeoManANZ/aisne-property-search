# Aisne (02) — Blocked Portals: Cracked via Residential Proxy

**Date:** 2026-08-18 · **Engine now:** direct → WARP → Lightpanda → stealth → **residential**

## The breakthrough

The missing ingredient for the "hard" blocks was a **residential egress IP**.
Every prior attempt (direct, WARP, Lightpanda, stealth-on-datacenter-IP)
failed on Orpi because cloud/datacenter IPs are pre-blocked regardless of
browser fingerprint.  Routing **stealth Chromium through a Webshare
residential proxy** gives both a clean fingerprint AND a residential IP.

**Verified live on Orpi** (the hardest — Cloudflare hard IP-block):
- direct: 403 · WARP: 403 · Lightpanda: block · stealth (datacenter): block
- **residential: ✅ 544,502 chars, price 194,900 €, surface 180 m² extracted**
- Cross-check vs known listing: **PASSED** (price 194900 = 194900, surface 180 = 180)

## Capability matrix (final, tested live)

| Portal | Block type | direct | warp | lightpanda | stealth | residential |
|---|---|---|---|---|---|---|
| FNAIM / iad / ParuVendu | none | ✅ | — | — | — | ✅ |
| **Orpi** | Cloudflare hard IP-block | ❌403 | ❌403 | ❌ | ❌ | ✅ **CRACKED** |
| **SeLoger** | DataDome interactive CAPTCHA | ❌403 | ❌403 | ❌ | ❌ shell | ❌ block |
| **Logic-Immo** | DataDome + CloudFront | ❌403 | ❌ | ❌ | ❌ shell | ❌ CF-403 |
| **Superimmo** | click-to-verify CAPTCHA | ❌503 | ❌ | — | ⚠️ captcha page | ⚠️ captcha |
| **Zilek** | Cloudflare JS challenge (stuck) | ❌ | ❌ | ❌ | ❌ loop | ❌ loop |

## What each remaining block needs

- **SeLoger / Logic-Immo (DataDome):** DataDome is an interactive CAPTCHA
  that detects automated browsers even on residential IPs.  Requires either
  a CAPTCHA-solving service (2captcha/anti-captcha) or a real human click.
  It also fingerprints the browser behaviourally, so even residential-IP
  headless browsers get challenged.
- **Superimmo:** a click-to-verify ("Prouvez que vous êtes un humain")
  button — needs a human click or solve service.
- **Zilek:** Cloudflare JS challenge that never auto-completes in headless
  Chromium (even with stealth + residential).  Needs a headed browser
  session or manual completion.

## Files

- `french-property-scraper.py` — now has a 5th engine: `residential`
  (stealth Chromium through rotating Webshare residential IPs).  Also
  tightened block-detection to flag DataDome/CloudFront/CAPTCHA shells
  that previously false-positived as "OK".
- `test_webshare.py`, `test_orpi_crack.py` — proof-of-concept scripts
- `.browsers/` — Firefox + ffmpeg (Chromium reused from /opt/data/.playwright-browsers)
- `.venv/` — requests, PySocks, playwright, playwright-stealth, browserbase

## Commands

```bash
# Full cascade (now includes residential as final tier)
cd /workspace/hermes1/projects/aisne-property-search/
.venv/bin/python french-property-scraper.py --engine cascade --input blocked_sites.txt --stealth-no-warp

# Residential engine only (for a known-hard IP-block site)
.venv/bin/python french-property-scraper.py --engine residential --url "https://www.orpi.com/..."
```

## Note on Browserbase

Browserbase residential proxies (env `BROWSERBASE_PROXIES=true`) require a
**paid plan** (free tier returns 402 Payment Required).  The Webshare
residential proxies are the working solution here.
