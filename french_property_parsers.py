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

import config

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

        # Location: data-title often has "TOWN NNNNN" at the end
        # e.g. "Immeuble  94m² HIRSON 02500" → "HIRSON (02500)"
        location = None
        # town = the LAST uppercase-alpha token sequence before the postcode,
        # excluding the surface (which contains digits/²)
        lm = re.search(r"\b([A-ZÀ-Ý][\wÀ-ÿ'\-]*(?:\s+[A-ZÀ-Ý][\wÀ-ÿ'\-]*)*)\s+(\d{5})\s*$", title)
        if lm:
            location = f"{lm.group(1).strip()} ({lm.group(2)})"
        else:
            # Fallback: the detail URL slug carries <town>-<postcode>.htm
            # e.g. /annonce-immobiliere/52722152/17-acheter-immeuble-hirson-02500.htm
            um = re.search(r"/immeuble-(.+?)-(\d{5})\.htm", href)
            if um:
                town = um.group(1).replace("-", " ").title()
                location = f"{town} ({um.group(2)})"

        # Full URL
        full_url = f"https://www.fnaim.fr{href}" if href.startswith("/") else href

        results.append({
            "source": "fnaim",
            "url": full_url,
            "title": title[:200],
            "price_eur": price,
            "surface_m2": surface,
            "price_per_m2": (price / surface) if (price and surface and surface > 0) else None,
            "location": location,
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

        # Location + surface from the URL slug:
        #   /annonce/immeuble-vente-<town>-<NNN>m2/r<id>
        location = None
        surface = None
        um = re.search(r"/immeuble-vente-([a-z0-9\-]+?)(?:-(\d+))?m2/", href)
        if um:
            town_slug = um.group(1).replace("-", " ").strip()
            if town_slug:
                location = town_slug.title()
            if um.group(2):
                try:
                    surface = int(um.group(2))
                except ValueError:
                    pass

        # Description: look for a meta description or og:description
        description = ""
        dm = re.search(r'<meta[^>]*name="description"[^>]*content="([^"]+)"', inner_html, re.IGNORECASE)
        if not dm:
            dm = re.search(r'<p[^>]*class="[^"]*description[^"]*"[^>]*>(.*?)</p>', inner_html, re.DOTALL | re.IGNORECASE)
        if dm:
            description = re.sub(r"<[^>]+>", " ", dm.group(1)).strip()

        results.append({
            "source": "iad",
            "url": full_url,
            "title": title[:200],
            "price_eur": price,
            "surface_m2": surface,
            "price_per_m2": (price / surface) if (price and surface and surface > 0) else None,
            "location": location,
            "description": description,
            "raw_title": title,
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "source_page": source_url,
        })

    return results


# ---------------------------------------------------------------------------
# PARUVENDU PARSER (card-level)
# ---------------------------------------------------------------------------
# ParuVendu search results are structured as <div class="blocAnnonce"> cards.
# Each card contains: an <a href="/immobilier/vente/immeuble/BASE36ID"> link,
# plus sibling elements holding price ("450 000 &euro;"), title ("Immeuble"),
# location ("Soissons (02)"), DPE ("D"), energy, and a real description.
# Parsing at the CARD level (not the link level) avoids the JS-fragment junk
# inside the <a> and captures location/description that link-level parsing
# missed entirely.

PV_CARD_RE = re.compile(
    r'<div[^>]*class="[^"]*blocAnnonce[^"]*"[^>]*data-id="([^"]+)"[^>]*>(.*?)(?=<div[^>]*class="[^"]*blocAnnonce|</div>\s*</div>\s*</div>\s*</div>|id="bloc_loader")',
    re.DOTALL | re.IGNORECASE,
)
PV_DETAIL_HREF_RE = re.compile(r'href="(/immobilier/vente/immeuble/[A-Z0-9]+)"', re.IGNORECASE)


def _pv_is_detail_url(url: str) -> bool:
    """True only if the URL points to an individual property (has a base36 ID)."""
    return bool(re.search(r"/immeuble/([A-Z0-9]{17,22})$", url))
PV_PRICE_RE = re.compile(r"([\d\s]{4,})\s*&euro;|([\d\s]{4,})\s*€", re.IGNORECASE)
PV_SURFACE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*m²", re.IGNORECASE)
# location appears as "Soissons (02)" — a proper town name followed by (NN) or NNNNN.
# Anchored: NOT preceded by "Immeuble" (which would grab the surface line).
PV_LOCATION_RE = re.compile(
    r"(?<!Immeuble\s)([A-ZÀ-Ý][\wÀ-ÿ'\- ]{2,}?)\s*\(?\s*(\d{2,5})\s*\)?",
    re.IGNORECASE,
)
PV_DPE_RE = re.compile(r"DPE\s*:\s*([A-G])", re.IGNORECASE)
PV_ENERGY_RE = re.compile(r"(\d{3})\s*kWh/m²\.an", re.IGNORECASE)


def _pv_card_text(card_html: str) -> str:
    """Visible text of a card (scripts stripped), collapsed to single spaces."""
    seg = re.sub(r"<script.*?</script>", " ", card_html, flags=re.DOTALL)
    seg = re.sub(r"<[^>]+>", " ", seg)
    seg = re.sub(r"\s+", " ", seg)
    return seg.strip()


def parse_paruvendu(html: str, source_url: str) -> list[dict]:
    """
    Parse ParuVendu listing page → list of property dicts.
    Uses lxml to split blocAnnonce cards and extract from real elements:
      - url: detail link
      - price: div.encoded-lnk (font-medium) containing '450 000 €'
      - title: detail link title attr ('Immeuble')
      - location: <a> with 'L'ADRESSE' text, or 'Town (NN)' in card text
      - DPE: span.NoteEnerg_X
      - description: p.text-justify (line-clamp-5)
      - surface: 'NNN m²' from card text
    Only individual property detail URLs are kept (base36-ID pattern).
    """
    try:
        from lxml import html as lhtml
    except ImportError:
        return []
    results = []
    seen = set()
    try:
        doc = lhtml.fromstring(html)
    except Exception:
        return []

    for card in doc.cssselect("div.blocAnnonce"):
        # detail link
        a = card.cssselect('a[href*="/immobilier/vente/immeuble/"]')
        if not a:
            continue
        href = a[0].get("href") or ""
        full_url = "https://www.paruvendu.fr" + href if href.startswith("/") else href
        if not _pv_is_detail_url(full_url):
            continue
        if full_url in seen:
            continue
        seen.add(full_url)

        title = (a[0].get("title") or "").strip()[:200]

        # price
        price = None
        price_el = card.cssselect("div.encoded-lnk")
        if price_el:
            ptxt = price_el[0].text_content()
            pm = re.search(r"([\d\s.]+)\s*€", ptxt)
            if pm:
                try:
                    price = int(pm.group(1).replace(" ", "").replace(".", "").replace(",", ""))
                except ValueError:
                    price = None
        if price is not None and price < 5000:
            price = None

        # full card text (collapsed)
        raw = lhtml.tostring(card, encoding="unicode")
        text = re.sub(r"<script.*?</script>", " ", raw, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)

        # surface: 'NNN m²' with plausible building size
        surface = None
        for sm in re.finditer(r"(\d+(?:\.\d+)?)\s*m²", text):
            v = int(float(sm.group(1)))
            if 20 <= v <= 5000:
                surface = v
                break

        # location: 'L'ADRESSE <TOWN>' or 'Town (NN)'
        location = None
        addr_el = card.cssselect("a[class*=line-clamp-1]")
        for ae in addr_el:
            t = ae.text_content().strip()
            if "adresse" in t.lower():
                loc = re.sub(r"^L'ADRESSE\s*", "", t, flags=re.IGNORECASE).strip()
                if loc:
                    location = loc[:120]
                    break
        if not location:
            lm = re.search(r"([A-ZÀ-Ý][\wÀ-ÿ'\-]{2,})\s*\((\d{2,5})\)", text)
            if lm:
                location = f"{lm.group(1).strip()} ({lm.group(2)})"

        # DPE: span with class NoteEnerg_X
        dpe = None
        for el in card.cssselect("[class*=NoteEnerg]"):
            t = el.get("class") or ""
            m = re.search(r"NoteEnerg_([A-G])", t)
            if m:
                dpe = m.group(1).upper()
                break
        if not dpe:
            dm = re.search(r"DPE\s*:\s*([A-G])", text)
            if dm:
                dpe = dm.group(1).upper()

        # description: p.text-justify
        description = ""
        p_el = card.cssselect("p.text-justify, p[class*=line-clamp-5]")
        if p_el:
            description = p_el[0].text_content().strip()

        results.append({
            "source": "paruvendu",
            "url": full_url,
            "title": title,
            "price_eur": price,
            "surface_m2": surface,
            "price_per_m2": (price / surface) if (price and surface and surface > 0) else None,
            "location": location,
            "dpe_energy": dpe,
            "description": description,
            "ref": None,
            "raw_text": text[:500],
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "source_page": source_url,
        })

    return results


# ---------------------------------------------------------------------------
# DETAIL PAGE SCANNER — get full listing data from individual ad pages
# ---------------------------------------------------------------------------

# NOTE: the heavy `french_property_scraper` module is imported LAZILY inside
# scan_detail_page() — it pulls in DataDome/2Captcha/stealth engines that are
# only needed for detail-page extraction.  Keeping the import out of module
# scope means the common ladder path (search-card parsing + DB upsert) never
# pays that load cost.

def scan_detail_page(url: str, source: str, timeout: int = 30) -> dict:
    """
    Fetch and extract data from an individual listing detail page.
    Returns the enriched extract_property_data() result plus source info.
    """
    # Lazy import: the scraper module is heavy (DataDome/2Captcha/stealth
    # engines) and only needed when detail extraction actually runs.
    from french_property_scraper import (
        extract_property_data,
        WarpSession,
        scrape_via_lightpanda,
        scrape_via_stealth,
    )

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
# SELOGER PRICE HELPER
# ---------------------------------------------------------------------------
# The DOM card parsers (parse_seloger / parse_ufrn) were removed — SeLoger
# is scraped via the BFF API in seloger_api_sweep.py.  This helper remains
# because both seloger_api_sweep.py and the old standalone scripts use it
# to normalise SeLoger price strings ("465 000 €" / "465 000 €").

# Price text: "465 000 €" or "1 009 €/m²" (narrow no-break space \u202f / \u00a0)
_SELOGER_PRICE_RE = re.compile(
    r"([\d][\d\s\u00a0\u202f.,]*)\s*€", re.IGNORECASE)


def _clean_seloger_price(raw: str) -> int | None:
    """'465 000 €' / '465\u202f000\u202f€' → 465000."""
    if not raw:
        return None
    s = raw.replace("\u202f", "").replace("\u00a0", "").replace(" ", "").replace(",", ".")
    # strip any trailing currency / suffix (€, EUR) that callers may include
    s = s.rstrip("€EUReur ")
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


# LADDER — crawl FNAIM + IAD + ParuVendu in sequence
# ---------------------------------------------------------------------------

PARSERS = {
    "fnaim": parse_fnaim,
    "iad": parse_iad,
    "paruvendu": parse_paruvendu,
    # NOTE: SeLoger is intentionally NOT wired into the ladder.  It is a JS
    # SPA behind DataDome; the definitive path is seloger_api_sweep.py
    # (BFF API).  parse_seloger / parse_ufrn below are kept for standalone
    # scripts but must NOT be run through the ladder's plain-HTTP fetch.
}

SOURCE_URLS = {
    "fnaim": "https://www.fnaim.fr/liste-annonces-immobilieres/17-acheter-immeuble-aisne-02.htm",
    "iad": "https://www.iadfrance.fr/annonces/aisne-02/vente/immeuble",
    "paruvendu": "https://www.paruvendu.fr/immobilier/vente/immeuble/soissons-02200/",
}

OUTPUT_DIR = Path(config.PROJECT_ROOT) / "scans"
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
        sources = ["fnaim", "iad", "paruvendu"]
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
            # PRICE precedence: the search-card (item) price is parsed from the
            # reliable listing card and is CORRECT. The detail-page price is
            # unreliable (grabs hidden/embedded numbers, e.g. 58,248 vs the real
            # 808,000) — so NEVER let it override a valid search-card price.
            # Only fall back to detail price if the search price is missing.
            item_price = item.get("price_eur") or item.get("price")
            detail_price = detail.get("price_eur")
            if item_price:
                price = item_price
            else:
                price = detail_price
            db_records.append({
                "url": url,
                "source": item.get("source") or detail.get("source"),
                "title": detail.get("title") or item.get("title"),
                "price_eur": price,
                "surface_m2": detail.get("surface_m2") or item.get("surface_m2"),
                "dpe_energy": detail.get("dpe_energy") or item.get("dpe_energy"),
                "location": detail.get("location") or item.get("location"),
                "agency": detail.get("agency") or item.get("agency"),
                "description": detail.get("description_snippet") or item.get("raw_text"),
            })
        db.bulk_upsert_validated(db_records)
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

    # Per-source commands (SeLoger excluded — use seloger_api_sweep.py)
    for src in ["fnaim", "iad", "paruvendu"]:
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

    if args.command in ("fnaim", "iad", "paruvendu"):
        src = args.command
        url = args.url or SOURCE_URLS.get(src)
        if not url:
            print(f"No URL for {src}"); sys.exit(1)

        print(f"Fetching {src}: {url} ...")
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
