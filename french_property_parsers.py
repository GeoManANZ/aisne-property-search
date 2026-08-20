"""
FRENCH PROPERTY LISTING PARSERS — FNAIM / iad / ParuVendu

Extracts property listings from searchable result pages (HTML → structured JSON).
Also traverses detail pages linked from search results.

Usage:
  python3 french-property-parsers.py fnaim --url "https://www.fnaim.fr/liste-annonces-immobilieres/17-acheter-immeuble-aisne-02.htm"
  python3 french-property-parsers.py iad --url "https://www.iadfrance.fr/annonces/aisne-02/vente/immeuble"
  python3 french-property-parsers.py paruvendu --url "https://www.paruvendu.fr/immobilier/vente/immeuble/soissons-02200/"
  python3 french-property-parsers.py ladder  # crawl all 3 in sequence
"""

import json
import re
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# HTML fetching helpers
# ---------------------------------------------------------------------------

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


def fetch(url: str, timeout: int = 30) -> str:
    """Fetch HTML from a URL (direct, no proxy needed for FNAIM/iad/ParuVendu)."""
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def fetch_seloger(url: str, timeout: int = 45) -> str:
    """Fetch a SeLoger search page through the DataDome-bypass engine.

    SeLoger is behind DataDome, so a plain requests.get gets a 403 challenge.
    We solve via 2Captcha (matching proxy + UA) and return the real SPA HTML.
    Falls back to the Camoufox render if the solve fails.
    """
    from french_property_scraper import scrape_via_datadome
    r = scrape_via_datadome(url, timeout_s=timeout)
    if r.success and len(r.html) > 3000:
        return r.html
    # Fallback: try Camoufox render (free)
    try:
        from french_property_scraper import scrape_via_camoufox
        cr = scrape_via_camoufox(url, timeout_s=timeout, use_proxy=True, warmup=True)
        if cr.get("success") and len(cr.get("rawHtml", "")) > 3000:
            return cr["rawHtml"]
    except Exception:
        pass
    raise RuntimeError(f"SeLoger fetch failed: {r.error or 'blocked'}")


# ---------------------------------------------------------------------------
# FNAIM PARSER
# ---------------------------------------------------------------------------
# FNAIM listing pages: https://www.fnaim.fr/liste-annonces-immobilieres/17-acheter-immeuble-aisne-02.htm
# Each listing is an <a class="lienannonce" href="/annonce-immobiliere/NNNNNNN/...">
#   Title: text of the <a> (includes "Achat [Type] [Location] [Price]")
#   Details: sibling block with surface, composition, DPE etc.

FNAIM_ANNONCE_RE = re.compile(
    r'<a[^>]*href="(/annonce-immobiliere/\d+/[^"]+)"[^>]*class="[^"]*linkAnnonce[^"]*"[^>]*data-title="([^"]*)"[^>]*>',
    re.IGNORECASE)
FNAIM_PRICE_RE = re.compile(r"(\d{2,3}(?:\.\d{3})*)\s*€", re.IGNORECASE)
FNAIM_SURFACE_RE = re.compile(r"(\d+)\s*m²", re.IGNORECASE)
FNAIM_LOCATION_RE = re.compile(r"<strong>([^<]+)</strong>", re.IGNORECASE)
FNAIM_NEXT_PAGE_RE = re.compile(r'href="([^"]*liste-annonces-immobilieres[^"]*page=\d+[^"]*)"', re.IGNORECASE)


def parse_fnaim(html: str, source_url: str) -> list[dict]:
    """
    Parse FNAIM listing page → list of property dicts.
    Each listing: url, title, price, surface, location, source_url
    """
    results = []
    for href, data_title in FNAIM_ANNONCE_RE.findall(html):
        # data-title attribute contains the listing title + surface
        # (e.g. "Immeuble 215m² BRANCOURT EN LAONNOIS 02320")
        title = data_title.strip()
        if not title:
            continue

        # Surface is often in data-title (e.g. "Immeuble 215m² ...")
        surface = None
        sm = FNAIM_SURFACE_RE.search(title)
        if sm:
            try:
                surface = int(sm.group(1))
            except ValueError:
                pass

        # Price: try extract from title first, then from href pattern
        price = None
        pm = FNAIM_PRICE_RE.search(title)
        if pm:
            try:
                price = int(pm.group(1).replace(".", ""))
            except ValueError:
                pass

        # Full URL
        full_url = f"https://www.fnaim.fr{href}" if href.startswith("/") else href

        results.append({
            "source": "fnaim",
            "url": full_url,
            "title": title[:200],
            "price_eur": price,
            "surface_m2": surface,
            "price_per_m2": (price / surface) if (price and surface and surface > 0) else None,
            "raw_title": title,
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "source_page": source_url,
        })

    return results


# ---------------------------------------------------------------------------
# IAD PARSER
# ---------------------------------------------------------------------------
# IAD listing pages: https://www.iadfrance.fr/annonces/aisne-02/vente/immeuble
# Uses Vue.js / Nuxt — listings embedded in page as JSON or rendered server-side.
# Each card: <a href="/annonce/immeuble-vente-XXX-YYY">
#   Title + price in card header

IAD_LINK_RE = re.compile(r'<a[^>]*href="(/annonce/[^"]+)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
IAD_CARD_PRICE_RE = re.compile(r"<strong>([\d\s]+)\s*€</strong>", re.IGNORECASE)
IAD_TITLE_RE = re.compile(r"<h[123][^>]*>(.*?)</h[123]>", re.DOTALL | re.IGNORECASE)


def parse_iad(html: str, source_url: str) -> list[dict]:
    """
    Parse IAD listing page → list of property dicts.
    IAD pages are Vue-rendered — the link hrefs are usually present in raw HTML
    even if titles are JS-rendered.
    """
    results = []
    seen = set()

    for href, inner_html in IAD_LINK_RE.findall(html):
        # Only care about /annonce/ links (detail pages, not navigation)
        if "/annonce/" not in href:
            continue
        full_url = f"https://www.iadfrance.fr{href}" if href.startswith("/") else href
        if full_url in seen:
            continue
        seen.add(full_url)

        # Try to extract price from nearby content
        price = None
        pm = IAD_CARD_PRICE_RE.search(inner_html)
        if pm:
            try:
                price = int(pm.group(1).replace(" ", ""))
            except ValueError:
                pass

        # Title from heading if present in raw HTML
        title = ""
        tm = IAD_TITLE_RE.search(inner_html)
        if tm:
            title = re.sub(r"<[^>]+>", " ", tm.group(1)).strip()

        # Fallback: use the href as a hint
        if not title:
            # Extract location hint from URL: /annonce/immeuble-vente-laon-230m2/r2041287
            path = href.strip("/")
            parts = path.split("/")
            if parts:
                loc = parts[-1] if len(parts) > 1 else parts[0]
                title = loc[:120]

        results.append({
            "source": "iad",
            "url": full_url,
            "title": title[:200],
            "price_eur": price,
            "surface_m2": None,  # IAD search pages rarely show surface in raw HTML
            "price_per_m2": None,
            "raw_title": title,
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "source_page": source_url,
        })

    return results


# ---------------------------------------------------------------------------
# PARUVENDU PARSER
# ---------------------------------------------------------------------------
# ParuVendu listing pages:
#   https://www.paruvendu.fr/immobilier/vente/immeuble/soissons-02200/
#   Each listing card: <a class="listercom" href="/immobilier/vente/immeuble/NUMBER...">
#   Price and surface in card body via data attributes or visible text

PV_LINK_RE = re.compile(
    r'<a[^>]*href="(/immobilier/vente/immeuble/[^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE
)
PV_PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*€", re.IGNORECASE)
PV_SURFACE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*m²", re.IGNORECASE)
PV_LOCATION_RE = re.compile(r"<span[^>]*class=\"[^\"]*adresse[^\"]*\"[^>]*>(.*?)</span>", re.DOTALL | re.IGNORECASE)


def parse_paruvendu(html: str, source_url: str) -> list[dict]:
    """
    Parse ParuVendu listing page → list of property dicts.
    """
    results = []
    seen = set()

    for href, inner_html in PV_LINK_RE.findall(html):
        full_url = f"https://www.paruvendu.fr{href}" if href.startswith("/") else href
        if full_url in seen:
            continue
        seen.add(full_url)

        # Clean inner text
        text = re.sub(r"<[^>]+>", " ", inner_html)
        text = re.sub(r"\s+", " ", text).strip()

        price = None
        pm = PV_PRICE_RE.search(text)
        if pm:
            try:
                price = int(pm.group(1).replace(".", "").replace(",", ""))
            except ValueError:
                pass

        surface = None
        sm = PV_SURFACE_RE.search(text)
        if sm:
            try:
                surface = float(sm.group(1))
                surface = int(surface)
            except ValueError:
                pass

        results.append({
            "source": "paruvendu",
            "url": full_url,
            "title": text[:200],
            "price_eur": price,
            "surface_m2": surface,
            "price_per_m2": (price / surface) if (price and surface and surface > 0) else None,
            "raw_text": text,
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "source_page": source_url,
        })

    return results


# ---------------------------------------------------------------------------
# DETAIL PAGE SCANNER — get full listing data from individual ad pages
# ---------------------------------------------------------------------------

# Single normal import of the scraper module (no importlib / exec_module).
# The scraper file is `french_property_scraper.py` (underscored) so it
# imports as a regular package module.
from french_property_scraper import (
    extract_property_data,
    WarpSession,
    scrape_via_lightpanda,
    scrape_via_stealth,
    is_cloudflare_block,
)


def scan_detail_page(url: str, source: str, timeout: int = 30) -> dict:
    """
    Fetch and extract data from an individual listing detail page.
    Returns the enriched extract_property_data() result plus source info.
    """
    html = ""
    engine_used = "direct"

    # Try direct first
    try:
        html = fetch(url)
        if len(html) > 500 and not is_cloudflare_block(html, 200):
            pass  # OK
    except Exception:
        html = ""

    # Fallback to WARP if blocked
    if not html or is_cloudflare_block(html, 200):
        try:
            sess = WarpSession()
            html = sess.get(url).text
            engine_used = "warp"
        except Exception:
            pass

    # Fallback to fastCRW if still blocked
    if not html or is_cloudflare_block(html, 200):
        try:
            result = scrape_via_lightpanda(url)
            if result.get("success") and result.get("rawHtml"):
                html = result["rawHtml"]
                engine_used = "lightpanda"
        except Exception:
            pass

    # Final fallback: Playwright stealth Chromium (beats WAF fingerprinting,
    # though interactive CAPTCHAs like DataDome will still block).
    if not html or is_cloudflare_block(html, 200):
        try:
            result = scrape_via_stealth(url, timeout_s=timeout)
            if result.get("success") and result.get("rawHtml"):
                html = result["rawHtml"]
                engine_used = "stealth"
        except Exception:
            pass

    if not html or is_cloudflare_block(html, 200):
        return {
            "url": url,
            "source": source,
            "engine": engine_used,
            "status": "blocked",
            "data": {"url": url, "scanned_at": datetime.now(timezone.utc).isoformat()},
        }

    data = extract_property_data(html, url)
    data["source"] = source
    data["engine"] = engine_used
    data["status"] = "ok"
    return {"url": url, "source": source, "engine": engine_used, "status": "ok", "data": data}


# ---------------------------------------------------------------------------
# EXTERNAL BLOCK DETECTION (reuse from scraper)
# ---------------------------------------------------------------------------

_BLOCK_PHRASES = [
    "attention required",
    "sorry, you have been blocked",
    "access denied",
    "just a moment",
    "challenge-platform",
    "_cf_chl_opt",
    "ray id",
    "checking your browser",
]


def is_cloudflare_block(html: str, status: int) -> bool:
    if status == 403 or status == 451:
        return True
    low = html.lower()
    for phrase in _BLOCK_PHRASES:
        if phrase in low:
            return True
    if '<title>just a moment' in low:
        return True
    if 'cf-chl-integrity' in low:
        return True
    return False


# ---------------------------------------------------------------------------
# SELOGER PARSER (multi-listing from search result page)
# ---------------------------------------------------------------------------
# SeLoger is a JS SPA: the search page is rendered by Camoufox (or fetched
# with a solved DataDome cookie) into real HTML.  Each listing is a card:
#
#   <div data-testid="classified-card-mfe-<CARD_ID>">
#     <a data-testid="card-mfe-covering-link-testid" href=".../detail.htm">
#     <div data-testid="cardmfe-price-testid">465 000 €</div>
#     <div data-testid="card-mfe-energy-performance-class">E</div>
#     <div data-testid="cardmfe-keyfacts-testid">461 m²</div>
#     <div data-testid="cardmfe-description-box-address">Château-Thierry (02400)</div>
#     <div data-testid="cardmfe-description-text-testid">Immeuble à vendre...</div>
#     <div data-testid="cardmfe-agency-publisher-*-test-id">CANDAT IMMOBILIER</div>
#   </div>
#
# We parse each card into the SAME dict shape as the other parsers so the
# consolidated listings / DB pipeline is source-agnostic.

# Price text: "465 000 €" or "1 009 €/m²" (narrow no-break space \u202f / \u00a0)
_SELOGER_PRICE_RE = re.compile(
    r"([\d][\d\s\u00a0\u202f.,]*)\s*€", re.IGNORECASE)


def _clean_seloger_price(raw: str) -> int | None:
    """'465 000 €' / '465\u202f000\u202f€' → 465000."""
    if not raw:
        return None
    s = raw.replace("\u202f", "").replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def parse_seloger(html: str, source_url: str = "") -> list[dict]:
    """Parse a SeLoger search result page → list of property dicts.

    Handles the SPA-rendered card markup (data-testid selectors).  Skips
    promoted/partner cards that lack a real detail URL.  Returns [] if the
    page is a challenge stub (DataDome / no cards).
    """
    if not html or len(html) < 3000:
        return []
    low = html.lower()
    if any(b in low for b in ("geochallenge", "captcha-delivery", "prouvez que",
                              "just a moment", "cf-chl-integrity")):
        return []  # challenge stub — no listings

    try:
        from lxml import html as _lhtml
        tree = _lhtml.fromstring(html)
    except Exception:
        return []

    cards = tree.cssselect('[data-testid^="classified-card-mfe-"]')
    results = []
    seen = set()

    for card in cards:
        # --- URL (skip partner/promoted cards with wl-cdp links) ---
        link = card.cssselect('[data-testid="card-mfe-covering-link-testid"]')
        href = link[0].get("href") if link else None
        if not href or "detail.htm" not in href:
            continue
        if not href.startswith("http"):
            href = "https://www.seloger.com" + href
        if href in seen:
            continue
        seen.add(href)

        # --- Price ---
        price = None
        pe = card.cssselect('[data-testid="cardmfe-price-testid"]')
        if pe:
            txt = " ".join(pe[0].itertext()).strip()
            pm = _SELOGER_PRICE_RE.search(txt)
            if pm:
                price = _clean_seloger_price(pm.group(1))

        # --- Surface (keyfacts: "461 m²") ---
        surface = None
        kf = card.cssselect('[data-testid="cardmfe-keyfacts-testid"]')
        if kf:
            ktxt = " ".join(kf[0].itertext()).strip()
            sm = re.search(r"(\d+)\s*m²", ktxt, re.I)
            if sm:
                try:
                    surface = int(sm.group(1))
                except ValueError:
                    pass

        # --- DPE energy ---
        dpe = None
        de = card.cssselect('[data-testid="card-mfe-energy-performance-class"]')
        if de:
            dtxt = " ".join(de[0].itertext()).strip()
            dm = re.search(r"\b([A-G])\b", dtxt)
            if dm:
                dpe = dm.group(1).upper()

        # --- Location / address ---
        location = None
        le = card.cssselect('[data-testid="cardmfe-description-box-address"]')
        if le:
            location = " ".join(le[0].itertext()).strip()[:200]

        # --- Title / description ---
        title = ""
        te = card.cssselect('[data-testid="cardmfe-description-text-testid"]')
        if te:
            title = " ".join(te[0].itertext()).strip()[:250]

        # --- Agency ---
        agency = None
        ae = card.cssselect('[data-testid^="cardmfe-agency-publisher-"]')
        if ae:
            agency = " ".join(ae[0].itertext()).strip()[:120] or None

        # --- Card id from container testid ---
        card_id = None
        tid = card.get("data-testid") or ""
        if tid.startswith("classified-card-mfe-"):
            card_id = tid.replace("classified-card-mfe-", "")

        results.append({
            "source": "seloger",
            "url": href,
            "title": title,
            "price_eur": price,
            "surface_m2": surface,
            "price_per_m2": (price / surface) if (price and surface and surface > 0) else None,
            "dpe_energy": dpe,
            "location": location,
            "agency": agency,
            "ref": card_id,
            "raw_title": title,
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "source_page": source_url,
        })

    return results


def parse_ufrn(html: str, source_url: str = "") -> list[dict]:
    """Parse a SeLoger SERP page via its embedded JSON state (the CORRECT way).

    SeLoger's micro-frontend embeds the full search state as JSON in the
    initial HTML (no browser rendering needed):
      - window["__UFRN_FETCHER__"] = JSON.parse("...") → cards + pagination
      - window["__UFRN_STORE__"]   = JSON.parse("...") → app/filter state

    Cards live at:
      data.classified-serp-init-data.pageProps.classifiedsData.<CARD_ID>
    each with id/location/hardFacts/energyClass/url/type/tags/...
    Pagination: pageProps.page, pageProps.totalCount.

    Falls back to parse_seloger (DOM) if the JSON isn't present.
    Returns the same dict shape as parse_seloger.
    """
    if not html or len(html) < 3000:
        return []
    low = html.lower()
    if any(b in low for b in ("geochallenge", "captcha-delivery", "prouvez que",
                              "just a moment", "cf-chl-integrity")):
        return []

    page_props = None
    m = re.search(r'window\["__UFRN_FETCHER__"\]\s*=\s*JSON\.parse\("(.+?)"\);',
                  html, re.DOTALL)
    if m:
        try:
            raw = m.group(1).encode("utf-8").decode("unicode_escape")
            d = json.loads(raw)
            page_props = (d.get("data", {})
                           .get("classified-serp-init-data", {})
                           .get("pageProps", {}))
        except Exception:
            page_props = None

    if not page_props:
        # structured JSON missing → fall back to DOM parsing
        return parse_seloger(html, source_url)

    classifieds = page_props.get("classifiedsData") or page_props.get("classifieds") or {}
    # Normalise: classifiedsData is a dict keyed by card id; classifieds may be a list
    cards_dict = {}
    if isinstance(classifieds, dict):
        cards_dict = classifieds
    elif isinstance(classifieds, list):
        for c in classifieds:
            if isinstance(c, dict) and c.get("id"):
                cards_dict[c["id"]] = c

    results = []
    seen = set()
    for cid, card in cards_dict.items():
        if not isinstance(card, dict):
            continue
        url = card.get("url") or ""
        if isinstance(url, dict):
            url = url.get("seoUrl") or url.get("href") or ""
        if url and not url.startswith("http"):
            url = "https://www.seloger.com" + url
        if url and url in seen:
            continue
        if url:
            seen.add(url)

        # price / surface from hardFacts
        price = surface = None
        hf = card.get("hardFacts") or {}
        if isinstance(hf, dict):
            price = hf.get("price") or hf.get("priceValue") or hf.get("mainPrice")
            if price is None:
                for v in hf.values():
                    if isinstance(v, dict) and (v.get("price") is not None or v.get("value") is not None):
                        price = v.get("price") or v.get("value"); break
            surface = hf.get("livingArea") or hf.get("area") or hf.get("surface")
            if surface is None:
                # hardFacts.facts is a list: [{"type":"overallSpace","value":"461 m²"},...]
                for f in hf.get("facts") or []:
                    if isinstance(f, dict):
                        ftype = str(f.get("type") or "").lower()
                        if any(x in ftype for x in ("space", "surface", "area", "size", "living")):
                            surface = f.get("splitValue") or f.get("value")
                            break
                if surface is None:
                    for v in hf.values():
                        if isinstance(v, dict) and v.get("livingArea") is not None:
                            surface = v["livingArea"]; break
            # dict-coerce: price may be {"value": "...", "ariaLabel": "465000 €"}
            if isinstance(price, dict):
                price = price.get("ariaLabel") or price.get("value") or price.get("formatted")
            if isinstance(surface, dict):
                surface = surface.get("value") or surface.get("ariaLabel")
            # number-coerce
            if isinstance(price, str):
                pm = re.search(r"[\d\s]{4,}", price)
                price = _clean_seloger_price(pm.group(0)) if pm else None
            if isinstance(surface, str):
                sm = re.search(r"(\d+)", surface)
                surface = int(sm.group(1)) if sm else None
            # sanity: a real immeuble is never <10 m² — drop bogus values
            if surface is not None and surface < 10:
                surface = None

        # location
        location = None
        loc = card.get("location") or {}
        if isinstance(loc, dict):
            location = (loc.get("label") or loc.get("city") or loc.get("name")
                        or loc.get("displayName"))
            if isinstance(location, dict):
                location = location.get("label") or str(location)

        # DPE
        dpe = card.get("energyClass")
        disp = card.get("display")
        if dpe is None and isinstance(disp, dict):
            dpe = disp.get("energy")
        if isinstance(dpe, dict):
            dpe = dpe.get("value")
        if dpe and isinstance(dpe, str):
            dm = re.search(r"\b([A-G])\b", dpe)
            dpe = dm.group(1).upper() if dm else None

        # title / description
        title = ""
        md = card.get("mainDescription") or card.get("description") or ""
        if isinstance(md, dict):
            title = md.get("text") or md.get("title") or ""
        elif isinstance(md, str):
            title = md
        title = str(title).strip()[:250]

        # agency
        agency = None
        prov = card.get("provider") or card.get("cardProvider") or {}
        if isinstance(prov, dict):
            agency = prov.get("title") or prov.get("name")

        results.append({
            "source": "seloger",
            "url": url,
            "title": title,
            "price_eur": price,
            "surface_m2": surface,
            "price_per_m2": (price / surface) if (price and surface and surface > 0) else None,
            "dpe_energy": dpe,
            "location": location,
            "agency": agency,
            "ref": cid,
            "raw_title": title,
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "source_page": source_url,
        })

    return results


# ---------------------------------------------------------------------------
# LADDER — crawl FNAIM + IAD + ParuVendu in sequence
# ---------------------------------------------------------------------------

PARSERS = {
    "fnaim": parse_fnaim,
    "iad": parse_iad,
    "paruvendu": parse_paruvendu,
    "seloger": parse_ufrn,   # UFRN JSON extraction (falls back to DOM)
}

SOURCE_URLS = {
    "fnaim": "https://www.fnaim.fr/liste-annonces-immobilieres/17-acheter-immeuble-aisne-02.htm",
    "iad": "https://www.iadfrance.fr/annonces/aisne-02/vente/immeuble",
    "paruvendu": "https://www.paruvendu.fr/immobilier/vente/immeuble/soissons-02200/",
    "seloger": "https://www.seloger.com/recherche/achat/immeuble/hauts-de-france/aisne-02/ad06fr2",
}

OUTPUT_DIR = Path("/workspace/hermes1/projects/aisne-property-search/scans")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def ladder(
    sources: list[str] = None,
    max_listings_per_source: int = 200,
    scan_details_for: list[str] = None,  # "all" or list of source names
    delay_s: float = 2.0,
) -> dict:
    """
    Crawl FNAIM + IAD + ParuVendu search pages, extract all listings,
    optionally scan detail pages for the most promising ones.

    Returns dict with per-source results and consolidated listings.
    """
    if sources is None:
        sources = ["fnaim", "iad", "paruvendu", "seloger"]
    if scan_details_for is None:
        scan_details_for = []

    scan_id = hashlib.md5(str(datetime.now(timezone.utc)).encode()).hexdigest()[:8]
    out_dir = OUTPUT_DIR / scan_id
    out_dir.mkdir(parents=True, exist_ok=True)

    all_results = {}
    consolidated = []
    log_lines = [
        f"=== Ladder crawl {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} ===",
        f"Sources: {', '.join(sources)}",
        f"Max listings per source: {max_listings_per_source}",
        "",
    ]

    for source in sources:
        url = SOURCE_URLS.get(source)
        if not url:
            log_lines.append(f"  ✗ {source}: no URL configured")
            continue

        log_lines.append(f"--- [{source}] {url} ---")
        t0 = time.time()

        try:
            if source == "seloger":
                html = fetch_seloger(url, timeout=45)
            else:
                html = fetch(url, timeout=30)
            parser = PARSERS.get(source)
            if not parser:
                log_lines.append(f"  ✗ No parser for {source}")
                continue

            listings = parser(html, url)
            # Deduplicate within source by URL
            seen_urls = set()
            unique = []
            for l in listings:
                if l["url"] not in seen_urls:
                    seen_urls.add(l["url"])
                    unique.append(l)
                if len(unique) >= max_listings_per_source:
                    break

            log_lines.append(f"  Parsed {len(unique)} listings ({len(listings)} raw, {len(seen_urls)} unique URLs)")
            all_results[source] = {
                "url": url,
                "listings_count": len(unique),
                "listings": unique,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "html_length": len(html),
            }
            consolidated.extend(unique)
            log_lines.append(f"  Time: {time.time() - t0:.1f}s")
            log_lines.append("")

            # Save raw HTML for this source
            (out_dir / f"{source}_search.html").write_text(html[:10_000_000], encoding="utf-8")

        except Exception as e:
            log_lines.append(f"  ✗ Error: {type(e).__name__}: {e}")
            log_lines.append("")
            all_results[source] = {"error": str(e), "url": url}

        time.sleep(delay_s)

    # Save consolidated JSON
    consolidated_path = out_dir / "consolidated_listings.json"
    # Strip raw_html/raw_title from consolidated for compactness
    slim = []
    for item in consolidated:
        slim_item = {k: v for k, v in item.items() if k not in ("raw_title", "raw_text")}
        slim.append(slim_item)
    consolidated_path.write_text(json.dumps(slim, indent=2, ensure_ascii=False), encoding="utf-8")

    log_lines.append(f"=== Total listings across sources: {len(consolidated)} ===")
    log_lines.append("")

    # Optional: scan detail pages for promising listings
    if scan_details_for:
        log_lines.append(f"--- Scanning detail pages for: {', '.join(scan_details_for)} ---")
        scan_dir = out_dir / "scanned_details"
        scan_dir.mkdir(parents=True, exist_ok=True)

        scanned = []
        for item in consolidated:
            src = item.get("source", "")
            if scan_details_for == "all" or src in scan_details_for:
                # Only scan if we have a useful URL and some expectation of data
                url = item.get("url", "")
                if url and len(url) > 20:
                    log_lines.append(f"  Scanning: {item.get('title','')[:60]}... → {url}")
                    try:
                        result = scan_detail_page(url, src, timeout=30)
                        scanned.append(result)
                        # Save detail HTML
                        safe = re.sub(r"[^\w\-]", "_", item.get("title", "listing")[:30])
                        (scan_dir / f"{safe}.html").write_text(
                            result.get("data", {}).get("raw_html", "")[:2_000_000],
                            encoding="utf-8"
                        )
                        log_lines.append(f"    → {result.get('status')} via {result.get('engine')}, "
                                          f"price={result['data'].get('price_eur')}, "
                                          f"surface={result['data'].get('surface_m2')} m², "
                                          f"dpe={result['data'].get('dpe_energy')}")
                    except Exception as e:
                        log_lines.append(f"    ✗ {type(e).__name__}: {e}")
                    time.sleep(delay_s)

        # Save scanned details
        scanned_path = out_dir / "scanned_details.json"
        scanned_path.write_text(json.dumps(scanned, indent=2, ensure_ascii=False), encoding="utf-8")
        log_lines.append(f"  Scanned {len(scanned)} detail pages")
        log_lines.append("")

    # ---- Persist to listings DB (SQLite) ----------------------------------
    # Every run upserts what it found so listings.db accumulates history.
    # Detail-scan results are richer, so we prefer those; otherwise fall back
    # to the consolidated (search-card) fields.
    try:
        from listings_db import ListingsDB
        db = ListingsDB(Path(__file__).parent / "listings.db")
        db_records = []
        detail_by_url = {r.get("url"): r.get("data", {})
                         for r in (scanned if scan_details_for else [])
                         if r.get("url")}
        for item in consolidated:
            url = item.get("url", "")
            if not url:
                continue
            detail = detail_by_url.get(url, {})
            db_records.append({
                "url": url,
                "source": item.get("source") or detail.get("source"),
                "title": detail.get("title") or item.get("title"),
                "price_eur": detail.get("price_eur") or item.get("price_eur") or item.get("price"),
                "surface_m2": detail.get("surface_m2") or item.get("surface_m2"),
                "dpe_energy": detail.get("dpe_energy") or item.get("dpe_energy"),
                "location": detail.get("location") or item.get("location"),
                "agency": detail.get("agency") or item.get("agency"),
                "description": detail.get("description_snippet") or item.get("raw_text"),
            })
        db.bulk_upsert(db_records)
        db.close()
        log_lines.append(f"  DB: upserted {len(db_records)} records (running total in listings.db)")
    except Exception as e:
        log_lines.append(f"  DB: skipped — {type(e).__name__}: {e}")

    # Save full log
    (out_dir / "ladder_log.txt").write_text("\n".join(log_lines), encoding="utf-8")

    return {
        "scan_id": scan_id,
        "results": all_results,
        "consolidated_count": len(consolidated),
        "consolidated": slim,
        "scanned_details": scanned if scan_details_for else [],
        "log_path": str(out_dir / "ladder_log.txt"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="French property listing parsers — FNAIM / IAD / ParuVendu"
    )
    subparsers = parser.add_subparsers(dest="command")

    # Per-source commands
    for src in ["fnaim", "iad", "paruvendu", "seloger"]:
        sp = subparsers.add_parser(src, help=f"Parse {src} search page")
        sp.add_argument("--url", help="Override default URL")
        sp.add_argument("--max", type=int, default=200, help="Max listings to extract")
        sp.add_argument("--detail-scan", action="store_true",
                        help="After parsing search page, scan detail pages for all listings")

    # Ladder command
    lp = subparsers.add_parser("ladder", help="Crawl all 3 sources in sequence")
    lp.add_argument("--sources", nargs="+", default=["fnaim", "iad", "paruvendu"],
                    help="Which sources to crawl")
    lp.add_argument("--max", type=int, default=200, help="Max listings per source")
    lp.add_argument("--detail-scan", nargs="?", const="all", default=None,
                    help="Scan detail pages: 'all' or list of source names (fnaim iad paruvendu)")
    lp.add_argument("--delay", type=float, default=2.0, help="Delay between requests (s)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command in ("fnaim", "iad", "paruvendu", "seloger"):
        src = args.command
        url = args.url or SOURCE_URLS.get(src)
        if not url:
            print(f"No URL for {src}"); sys.exit(1)

        print(f"Fetching {src}: {url} ...")
        if src == "seloger":
            html = fetch_seloger(url)
        else:
            html = fetch(url)
        parser = PARSERS[src]
        listings = parser(html, url)

        # Deduplicate
        seen = set()
        uniq = []
        for l in listings:
            if l["url"] not in seen:
                seen.add(l["url"])
                uniq.append(l)
        listings = uniq[:args.max]

        scan_details = args.detail_scan

        print(f"\nParsed {len(listings)} listings from {src}")
        for i, l in enumerate(listings[:20], 1):
            price_str = f"{l['price_eur']:,} €" if l["price_eur"] else "—"
            surf_str = f"{l['surface_m2']} m²" if l["surface_m2"] else "—"
            psm = f"{l['price_per_m2']:.0f} €/m²" if l["price_per_m2"] else "—"
            print(f"  {i:3d}. {price_str:>10} | {surf_str:>8} | {psm:>7} | {l['title'][:70]}")
            print(f"       {l['url']}")

        if len(listings) > 20:
            print(f"  ... and {len(listings) - 20} more")

        # Save to file
        scan_id = hashlib.md5(str(datetime.now(timezone.utc)).encode()).hexdigest()[:8]
        out_dir = OUTPUT_DIR / scan_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "search_listings.json").write_text(
            json.dumps(listings, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        if scan_details:
            print(f"\n--- Scanning {len(listings)} detail pages ---")
            scanned = []
            detail_dir = out_dir / "details"
            detail_dir.mkdir(parents=True, exist_ok=True)
            for item in listings:
                try:
                    result = scan_detail_page(item["url"], src, timeout=30)
                    scanned.append(result)
                    status_icon = "✅" if result["status"] == "ok" else "❌"
                    data = result.get("data", {})
                    print(f"  {status_icon} {item['title'][:50]}")
                    print(f"     price={data.get('price_eur')}  surface={data.get('surface_m2')}  DPE={data.get('dpe_energy')}  €/m²={data.get('price_per_m2')}")
                    print(f"     via {result.get('engine')}")
                except Exception as e:
                    print(f"  ❌ Error: {e}")

            (out_dir / "scanned_details.json").write_text(
                json.dumps(scanned, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"\nScanned {len(scanned)} detail pages → {out_dir}/scanned_details.json")

        print(f"\nSaved to: {out_dir}/")

    elif args.command == "ladder":
        scan_sources = args.sources or ["fnaim", "iad", "paruvendu"]
        scan_details_for = args.detail_scan  # "all" or list

        print(f"Ladder crawl: {', '.join(scan_sources)}")
        if scan_details_for:
            print(f"  Detail scan: {scan_details_for}")
        print()

        result = ladder(
            sources=scan_sources,
            max_listings_per_source=args.max,
            scan_details_for=scan_details_for,
            delay_s=args.delay,
        )

        print(f"\n=== RESULTS ===")
        print(f"Scan ID: {result['scan_id']}")
        print(f"Total listings: {result['consolidated_count']}")
        for src, data in result["results"].items():
            count = data.get("listings_count", 0) if isinstance(data, dict) else 0
            err = data.get("error", "") if isinstance(data, dict) else ""
            print(f"  {src:10s}: {count} listings  {'⚠ ' + err if err else ''}")

        if result["scanned_details"]:
            print(f"\nScanned {len(result['scanned_details'])} detail pages")
            ok_count = sum(1 for s in result["scanned_details"] if s.get("status") == "ok")
            print(f"  {ok_count} OK, {len(result['scanned_details']) - ok_count} blocked/failed")
            # Top priced
            ok_scanned = [s for s in result["scanned_details"] if s.get("status") == "ok"]
            ok_scanned.sort(key=lambda x: x.get("data", {}).get("price_eur") or 0)
            print("\n  Scanned listings (by price):")
            for s in ok_scanned[:15]:
                d = s.get("data", {})
                pe = d.get("price_eur")
                sm = d.get("surface_m2")
                pp = d.get("price_per_m2")
                dpe = d.get("dpe_energy")
                pe_str = f"{pe:,} €" if pe else "—"
                sm_str = f"{sm} m²" if sm else "—"
                pp_str = f"{pp:.0f} €/m²" if pp else "—"
                # Link included in the table output so the user can click
                # straight to the portal listing
                print(f"    {pe_str:>10} | {sm_str:>8} | DPE {dpe or '?':>3} | {pp_str:>8} | "
                      f"{s.get('source','?'):7s} | {s.get('url','')}")

        print(f"\nLog: {result['log_path']}")
