"""SeLoger full sweep via the BFF API — the DEFINITIVE pipeline.

Reverse-engineered 2026-08-20:
  POST https://www.seloger.com/serp-bff/search
    Body: {"criteria": {...search model...}, "paging": {"page": N, "size": 30, "order": "Default"}}
    Resp: {"totalCount": 257, "classifieds": [{"id": "..."}, ...]}
  GET  https://www.seloger.com/classifiedList/<id1>,<id2>,...
    Resp: [full card objects]

Flow:
  1. Camoufox browser + Webshare French sticky session loads the SERP once
     (cycles sessions until the page has REAL content — no challenge).
     This earns the datadome cookie in the browser context.
  2. From the browser context, fetch the API: POST serp-bff/search for each
     page, then GET classifiedList for the ids. The cookie rides along.
  3. Full structured cards → listings.db.

This avoids: HTML parsing, click timing, DOM state races.
"""
import sys, re, time, json, argparse
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, Path(__file__).parent.as_posix())

from french_property_parsers import _clean_seloger_price

BASE_URL = ("https://www.seloger.com/recherche/achat/immeuble/"
            "hauts-de-france/aisne-02/ad06fr2")
CRITERIA = {
    "estateSubTypes": [], "portals": ["SL"], "furnished": [],
    "featuresIncluded": [], "projectTypes": ["New_Build", "Resale"],
    "buildState": [], "locationsInBuildingIncluded": [],
    "locationsInBuildingExcluded": [], "energyCertificateClass": [],
    "distributionTypes": ["Buy"], "estateTypes": ["Building"],
    "location": {"placeIds": ["AD06FR2"]}, "texts": [],
}


def card_to_listing(c: dict) -> dict:
    """Convert a classifiedList card → listing dict (same shape as parsers).

    Card schema (verified 2026-08-20 from classifiedList raw response):
      location.address.city / zipCode
      hardFacts.title, hardFacts.price.value (dict), hardFacts.facts (list)
      tracking.price (clean int), tracking.city
      rawData.surface.main / plot, rawData.price, rawData.providercity
      mainDescription.description, energyClass, url, provider/cardProvider
    """
    # --- price: prefer tracking.price (clean int), fall back to hardFacts ---
    price = c.get("tracking", {}).get("price") if isinstance(c.get("tracking"), dict) else None
    if price is None:
        try:
            price = c.get("rawData", {}).get("price")
        except Exception:
            price = None
    if price is None:
        hf = c.get("hardFacts") or {}
        if isinstance(hf, dict):
            p = hf.get("price")
            if isinstance(p, dict):
                price = p.get("value") or p.get("ariaLabel") or p.get("formatted")
            else:
                price = p
    if isinstance(price, str):
        pm = re.search(r"[\d\s]{4,}", price)
        price = _clean_seloger_price(pm.group(0)) if pm else None

    # --- surface: rawData.surface.main / plot, else hardFacts.facts ---
    surface = None
    rd = c.get("rawData") or {}
    if isinstance(rd, dict):
        surf = rd.get("surface")
        if isinstance(surf, dict):
            surface = surf.get("main") or surf.get("plot")
    if surface is None:
        hf = c.get("hardFacts") or {}
        if isinstance(hf, dict):
            for f in hf.get("facts") or []:
                if isinstance(f, dict):
                    ftype = str(f.get("type") or "").lower()
                    if any(x in ftype for x in ("space", "surface", "area", "size", "living")):
                        surface = f.get("splitValue") or f.get("value")
                        break
    if isinstance(surface, str):
        sm = re.search(r"(\d+)", surface)
        surface = int(sm.group(1)) if sm else None
    if surface is not None and surface < 10:
        surface = None

    # --- location ---
    location = None
    loc = c.get("location") or {}
    if isinstance(loc, dict):
        addr = loc.get("address") or {}
        if isinstance(addr, dict):
            parts = [addr.get("city"), addr.get("zipCode")]
            location = " ".join(str(x) for x in parts if x)
    if not location:
        tc = c.get("tracking") or {}
        if isinstance(tc, dict) and tc.get("city"):
            location = tc["city"]
    if not location:
        rd = c.get("rawData") or {}
        if isinstance(rd, dict) and rd.get("providercity"):
            location = rd["providercity"]

    # --- DPE ---
    dpe = c.get("energyClass")
    if isinstance(dpe, dict):
        dpe = dpe.get("value")
    if dpe and isinstance(dpe, str):
        dm = re.search(r"\b([A-G])\b", dpe)
        dpe = dm.group(1).upper() if dm else None

    # --- url: canonical = https://www.seloger.com/<legacyId>/detail.htm ---
    # Always prefer the legacyId form (clean, resolves correctly).  The raw
    # `url` field is sometimes /wl-cdp/ (promoted CDP) or a category-style
    # path that redirects to the wrong property.
    legacy_id = c.get("metadata", {}).get("legacyId") if isinstance(c.get("metadata"), dict) else None
    if legacy_id:
        url = f"https://www.seloger.com/{legacy_id}/detail.htm"
    else:
        url = c.get("url") or ""
        if isinstance(url, dict):
            url = url.get("seoUrl") or url.get("href") or ""
        if url and not url.startswith("http"):
            url = "https://www.seloger.com" + url
        # strip promoted walled-CDP links → canonical detail if an id is present
        m = re.search(r"/(\d{6,12})/(?:detail\.htm)?$", url)
        if m:
            url = f"https://www.seloger.com/{m.group(1)}/detail.htm"

    # --- title (short) / description (long) ---
    title = ""
    hf = c.get("hardFacts") or {}
    if isinstance(hf, dict):
        title = hf.get("title") or ""
    description = ""
    md = c.get("mainDescription") or {}
    if isinstance(md, dict):
        description = md.get("description") or md.get("headline") or ""
        if not title:
            title = md.get("headline") or ""
    elif isinstance(md, str):
        description = md
    title = str(title).strip()[:250]
    description = str(description).strip()

    # --- agency ---
    agency = None
    prov = c.get("cardProvider") or c.get("provider") or {}
    if isinstance(prov, dict):
        agency = prov.get("title") or prov.get("name")

    # --- features / tags (structured enrichment) ---
    features = []
    hf = c.get("hardFacts") or {}
    if isinstance(hf, dict):
        for f in hf.get("facts") or []:
            if isinstance(f, dict) and f.get("label"):
                val = f.get("value") or f.get("splitValue") or ""
                features.append(f"{f.get('label')} {val}".strip())
        if hf.get("keyfacts"):
            features.extend(hf["keyfacts"])
    tags = c.get("tags") or {}
    if not isinstance(tags, dict):
        tags = {}
    # gallery count
    gal = c.get("gallery") or {}
    n_photos = 0
    if isinstance(gal, dict) and isinstance(gal.get("images"), list):
        n_photos = len(gal["images"])

    return {
        "source": "seloger",
        "url": url,
        "title": title,
        "price_eur": price,
        "surface_m2": surface,
        "price_per_m2": (price / surface) if (price and surface and surface > 0) else None,
        "dpe_energy": dpe,
        "location": location,
        "agency": agency,
        "description": description,
        "features": json.dumps(features, ensure_ascii=False) if features else None,
        "tags": json.dumps({**tags, "photos": n_photos}, ensure_ascii=False) if tags or n_photos else None,
        "ref": c.get("id"),
        "raw_title": title,
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "source_page": BASE_URL,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="seloger_pages")
    ap.add_argument("--max-pages", type=int, default=9)
    ap.add_argument("--sessions", type=int, default=8)
    args = ap.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)

    from config import WEBSHARE_PROXY_PASS, WEBSHARE_ROTATE_HOSTS
    host = WEBSHARE_ROTATE_HOSTS[0]
    from camoufox.sync_api import Camoufox

    listings = []
    seen = set()

    for session in range(1, args.sessions + 1):
        proxy_url = f"http://ualfuslo-fr-{session}:{WEBSHARE_PROXY_PASS}@{host}"
        print(f"=== session fr-{session} ===", flush=True)
        launch = {"headless": True, "locale": "fr-FR", "humanize": True,
                  "geoip": False, "proxy": {"server": proxy_url}}
        try:
            with Camoufox(**launch) as browser:
                ctx = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris",
                                          viewport={"width": 1440, "height": 900})
                page = ctx.new_page()
                ok = False
                for attempt in range(3):
                    try:
                        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
                        ok = True
                        break
                    except Exception as e:
                        print(f"  goto {attempt+1}: {type(e).__name__}", flush=True)
                        time.sleep(5)
                if not ok:
                    continue
                page.wait_for_timeout(8000)
                html = page.content()
                if len(html) < 3000 or any(b in html.lower() for b in
                                           ("geochallenge", "captcha-delivery", "prouvez")):
                    print(f"  fr-{session} IP flagged — next", flush=True)
                    continue

                # ---- API sweep from inside the browser context ----
                total = None
                for pnum in range(1, args.max_pages + 1):
                    payload = {"criteria": CRITERIA,
                               "paging": {"page": pnum, "size": 30, "order": "Default"}}
                    try:
                        resp = page.evaluate(
                            """async (payload) => {
                                const r = await fetch('https://www.seloger.com/serp-bff/search', {
                                    method: 'POST',
                                    headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                                    body: JSON.stringify(payload)
                                });
                                if (!r.ok) return null;
                                return await r.json();
                            }""", payload)
                    except Exception as e:
                        print(f"  page {pnum} fetch err: {type(e).__name__}", flush=True)
                        break
                    if not resp:
                        print(f"  page {pnum}: null response", flush=True)
                        break
                    total = resp.get("totalCount") or total
                    ids = [c.get("id") for c in resp.get("classifieds", []) if c.get("id")]
                    print(f"  page {pnum}: {len(ids)} ids (total {total})", flush=True)
                    if not ids:
                        break
                    # fetch full cards in batches of 20
                    new = 0
                    for i in range(0, len(ids), 20):
                        batch = ids[i:i+20]
                        try:
                            cards = page.evaluate(
                                """async (ids) => {
                                    const r = await fetch('https://www.seloger.com/classifiedList/' + ids.join(','), {
                                        headers: {'Accept': 'application/json'}
                                    });
                                    if (!r.ok) return null;
                                    const txt = await r.text();
                                    try { return JSON.parse(txt); }
                                    catch (e) { return null; }
                                }""", batch)
                        except Exception as e:
                            print(f"  batch err: {type(e).__name__}", flush=True)
                            continue
                        if not cards:
                            print(f"  batch null — skipping", flush=True)
                            continue
                        for c in cards:
                            if not isinstance(c, dict):
                                continue
                            l = card_to_listing(c)
                            if l["url"] and l["url"] not in seen:
                                seen.add(l["url"])
                                listings.append(l)
                                new += 1
                    print(f"    → {new} new listings (total {len(listings)})", flush=True)
                print(f"  session fr-{session} done: {len(listings)} listings", flush=True)
                if listings:
                    break
        except Exception as e:
            print(f"  fr-{session} error: {type(e).__name__}: {str(e)[:60]}", flush=True)

    out = outdir / "all_listings_api.json"
    out.write_text(json.dumps(listings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {len(listings)} unique listings → {out}")
    try:
        from listings_db import ListingsDB
        db = ListingsDB(Path(__file__).parent / "listings.db")
        accepted, rejected = db.bulk_upsert_validated(listings)
        print(f"DB upsert: {accepted} accepted, {rejected} rejected (total {db.count()})")
        db.close()
    except Exception as e:
        print(f"DB upsert failed: {type(e).__name__}: {str(e)[:80]}")


if __name__ == "__main__":
    main()
