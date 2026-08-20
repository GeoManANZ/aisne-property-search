"""SeLoger full pagination sweep — click through all SERP pages in Camoufox.

SeLoger's search results paginate via JS buttons (`aria-label="à la page N"`),
NOT URL params (`?page=2` is ignored — verified).  So the reliable way to get
all listings is:

  1. solve DataDome once via 2Captcha → datadome cookie (IP-bound)
  2. launch Camoufox through the WARP tunnel
  3. inject the cookie, navigate to the search URL
  4. wait for page 1 cards, capture the DOM
  5. click the NEXT button repeatedly; after each click wait for the new
     cards (SERP container re-renders), capture the DOM
  6. stop when the next button disappears or no new cards appear

Usage:
  python seloger_paginate.py [--url URL] [--outdir seloger_pages]
                             [--max-pages 20] [--proxy IP:PORT]

Saves per-page HTML to outdir/page_<N>.html and a consolidated listings
JSON to outdir/all_listings.json.  One 2Captcha solve total (~$0.003).
"""
import sys, re, json, time, argparse
from pathlib import Path
sys.path.insert(0, Path(__file__).parent.as_posix())

from twocaptcha_client import solve_datadome, get_balance
from config import (
    WEBSHARE_PROXY_USER, WEBSHARE_PROXY_PASS, WEBSHARE_PROXY_LIST,
    build_fingerprint,
)
from french_property_scraper import _parse_datadome_dd, _build_datadome_challenge_url
from french_property_parsers import parse_seloger
from webshare_chain import ChainedWebshareSession

UA = build_fingerprint()["user_agent"]

# SeLoger SERP lazy-loads cards as you scroll; each page has up to ~32 cards.
SCROLL_PASSES = 6          # wheel scrolls to trigger lazy rendering
SCROLL_WAIT_S = 1.2
PAGE_SETTLE_S = 4.0        # wait after clicking next, for the SPA to fetch


def solve_datadome_for(url, session=3, country="fr"):
    """Solve DataDome for a URL via a French sticky session.

    Returns (cookie_value, status, html).  Uses the SAME session for the
    challenge fetch and the SOCKS5 solve so the cookie matches the egress IP.
    """
    from webshare_chain import RotatingWebshareSession
    s = RotatingWebshareSession(session=session, timeout=45, country=country,
                                headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    r = s.get(url)
    dd = _parse_datadome_dd(r.text)
    if not dd:
        return None, r.status_code, r.text
    captcha_url = _build_datadome_challenge_url(dd, url)
    cookie_set = solve_datadome(captcha_url=captcha_url, page_url=url,
                                user_agent=UA, proxy=s.proxy_auth_str(),
                                proxytype="socks5")
    mm = re.search(r"datadome=([^;]+)", cookie_set or "")
    return (mm.group(1) if mm else None), r.status_code, r.text


def click_next(page) -> bool:
    """Click the 'page suivante' button.  Uses a REAL mouse click at the
    button's coordinates (proven to advance the SPA), scrolling it into view
    first.  Falls back to Playwright locator click, then DOM click."""
    try:
        # scroll the button into view
        page.evaluate("""() => {
            const b = document.querySelector('button[aria-label="page suivante"]');
            if (b) b.scrollIntoView({block: 'center'});
        }""")
        page.wait_for_timeout(800)
        pos = page.evaluate("""() => {
            const b = document.querySelector('button[aria-label="page suivante"]');
            if (!b) return null;
            const r = b.getBoundingClientRect();
            return {x: r.x + r.width/2, y: r.y + r.height/2, inView: r.top>=0 && r.bottom<=innerHeight};
        }""")
        if pos and pos['inView']:
            page.mouse.click(pos['x'], pos['y'])
            return True
    except Exception:
        pass
    # fallback: Playwright locator click
    try:
        btn = page.locator('button[aria-label="page suivante"]').first
        btn.click(timeout=5000, force=True)
        return True
    except Exception:
        pass
    # last resort: raw DOM click
    try:
        return bool(page.evaluate("""() => {
            const b = document.querySelector('button[aria-label="page suivante"]');
            if (b) { b.click(); return true; }
            return false;
        }"""))
    except Exception:
        return False


def current_page_num(page) -> int:
    """Return the highlighted/current page number, or 0 if unknown."""
    try:
        # the current page button has aria-label containing 'page actuelle'
        cur = page.locator('button[aria-label*="page actuelle"]').first
        if cur.is_visible(timeout=2000):
            return int(cur.text_content().strip())
    except Exception:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://www.seloger.com/recherche/achat/immeuble/hauts-de-france/aisne-02/ad06fr2")
    ap.add_argument("--outdir", default="seloger_pages")
    ap.add_argument("--proxy", default="fr-3",
                    help="Webshare rotating session (e.g. 'fr-3' = French sticky #3)")
    ap.add_argument("--max-pages", type=int, default=20)
    args = ap.parse_args()

    # parse the session spec: 'fr-3' → country='fr', session=3
    _cspec = args.proxy.split("-") if "-" in args.proxy else ["fr", args.proxy]
    country = _cspec[0]
    try:
        session = int(_cspec[1])
    except (IndexError, ValueError):
        session = 3

    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)
    print(f"balance before: {get_balance()}")

    # Step 1: solve DataDome once via the French sticky session
    cookie, status, html = solve_datadome_for(args.url, session=session, country=country)
    print(f"datadome cookie: {'solved ' + cookie[:20] + '...' if cookie else 'none (status ' + str(status) + ')'}")

    from camoufox.sync_api import Camoufox
    # The browser must egress from the SAME French residential IP that earned
    # the datadome cookie.  Point it at the local chained SOCKS server which
    # forwards to the rotating session (started separately, see README).
    launch = {"headless": True, "locale": "fr-FR", "humanize": True,
              "geoip": False,  # skip IP self-check; chain server provides it
              "proxy": {"server": "socks5://127.0.0.1:1081"}}

    all_listings = []
    seen_urls = set()
    pages_saved = 0
    last_page = 0

    with Camoufox(**launch) as browser:
        context = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris",
                                      viewport={"width": 1440, "height": 900})
        page = context.new_page()
        if cookie:
            context.add_cookies([{
                "name": "datadome", "value": cookie, "domain": ".seloger.com",
                "path": "/", "secure": True}])
            print("injected datadome cookie")

        print(f"navigating to {args.url}...")
        for attempt in range(3):
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
                break
            except Exception as e:
                print(f"  goto attempt {attempt+1} failed: {type(e).__name__}: {str(e)[:80]}")
                if attempt == 2:
                    raise
                page.wait_for_timeout(3000)
        page.wait_for_timeout(int(PAGE_SETTLE_S * 1000))

        for page_idx in range(1, args.max_pages + 1):
            # scroll to trigger lazy card loading
            for _ in range(SCROLL_PASSES):
                page.mouse.wheel(0, 1200)
                page.wait_for_timeout(int(SCROLL_WAIT_S * 1000))

            html = page.content()
            (outdir / f"page_{page_idx:02d}.html").write_text(html, encoding="utf-8")
            pages_saved += 1

            listings = parse_seloger(html, args.url)
            new = 0
            for l in listings:
                if l["url"] not in seen_urls:
                    seen_urls.add(l["url"])
                    all_listings.append(l)
                    new += 1
            pn = current_page_num(page)
            print(f"  page {page_idx}: {len(listings)} cards ({new} new) — "
                  f"running total {len(all_listings)} [SPA says page {pn}]")

            # stop conditions
            if not listings:
                print("  no cards on this page — stopping")
                break
            if page_idx == last_page + 1 and pn == last_page:
                print("  page number didn't advance — stopping")
                break
            last_page = pn

            if page_idx >= args.max_pages:
                break

            # click next
            clicked = click_next(page)
            if not clicked:
                print("  no 'page suivante' button — last page reached")
                break
            # wait for the page number to advance AND cards to reappear
            # (page 2 loads async: cards drop to 0 briefly during the fetch)
            advanced = False
            for _ in range(30):
                page.wait_for_timeout(1000)
                pn_now = current_page_num(page)
                ncards_now = page.evaluate(
                    """() => document.querySelectorAll('[data-testid^="classified-card-mfe-"]').length""")
                if pn_now > pn and ncards_now > 0:
                    advanced = True
                    break
            if not advanced:
                print(f"  page number stuck at {pn} after clicking next — stopping")
                break
            page.wait_for_timeout(int(PAGE_SETTLE_S * 1000))
            # hard guard against runaway
            if page_idx >= args.max_pages:
                break

    # Save consolidated listings
    out = outdir / "all_listings.json"
    out.write_text(json.dumps(all_listings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {len(all_listings)} unique listings → {out}")
    print(f"Pages saved: {pages_saved}")
    print(f"balance after: {get_balance()}")


if __name__ == "__main__":
    main()
