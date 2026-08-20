"""SeLoger full pagination — Camoufox + French residential IP + real clicks.

VALIDATED 2026-08-20 (live):
  - Camoufox + Webshare French sticky IP (ualfuslo-fr-N) passes DataDome
    WITHOUT any 2Captcha solve — no challenge, real cards.
  - Pagination is client-side: clicking the `à la page N` button advances
    (verified page 1 → 2 with new listing ids). `?LISTING-LISTpg=` URL param
    is IGNORED on the new /recherche/... URL format.
  - Direct backbone proxy (http://ualfuslo-fr-N:pass@host:80) is reliable;
    the chained SOCKS server was the flaky link.

No solves, no WARP. One browser session, real mouse clicks on page buttons.

Usage:
  python seloger_paginate_browser.py [--max-pages 9] [--session 3]
"""
import sys, re, time, json, argparse
from pathlib import Path
sys.path.insert(0, Path(__file__).parent.as_posix())

from french_property_parsers import parse_ufrn

BASE_URL = ("https://www.seloger.com/recherche/achat/immeuble/"
            "hauts-de-france/aisne-02/ad06fr2")
SCROLL_PASSES = 6
SCROLL_WAIT_S = 0.6
PAGE_SETTLE_S = 6.0


def click_page(page, target_num: int) -> bool:
    """Click the `à la page N` button.

    PROVEN recipe (verified live):
      1. scrollIntoView({block:'center'}) — the nav sits ~14,000px down
      2. native DOM `b.click()` — fires React's synthetic handler reliably
    Playwright's locator.click() FAILS on pages 3+ (element "not stable"
    during the SPA re-render, 8s timeout).  Raw mouse.click at coordinates
    also fails (overlay hit-testing).  JS .click() wins.
    """
    try:
        n = page.evaluate("""(n) => {
            const b = document.querySelector('button[aria-label="à la page ' + n + '"]');
            if (!b) return 'no-btn';
            b.scrollIntoView({block: 'center'});
            b.click();
            return 'clicked';
        }""", target_num)
        return n == "clicked"
    except Exception:
        pass
    # fallback: Playwright locator click
    try:
        page.evaluate("""(n) => {
            const b = document.querySelector('button[aria-label="à la page ' + n + '"]');
            if (b) b.scrollIntoView({block: 'center'});
        }""", target_num)
        page.wait_for_timeout(800)
        btn = page.locator(f'button[aria-label="à la page {target_num}"]').first
        btn.click(timeout=8000)
        return True
    except Exception:
        pass
    return False


def current_page(page) -> int:
    """Current page number from the 'page actuelle' button's ARIA label.

    The label is 'page actuelle, page 1' — the number lives in the aria-label,
    NOT the text content (which is empty)."""
    try:
        info = page.evaluate("""() => {
            const b = document.querySelector('button[aria-label*="page actuelle"]');
            return b ? b.getAttribute('aria-label') : null;
        }""")
        if info:
            m = re.search(r'(\d+)', info)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=BASE_URL)
    ap.add_argument("--outdir", default="seloger_pages")
    ap.add_argument("--max-pages", type=int, default=9)
    ap.add_argument("--sessions", type=int, default=8,
                    help="French sticky sessions to try (fr-1..fr-N)")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)

    from config import WEBSHARE_PROXY_PASS, WEBSHARE_ROTATE_HOSTS
    host = WEBSHARE_ROTATE_HOSTS[0]
    from camoufox.sync_api import Camoufox

    all_listings, seen = [], set()
    total_count = None

    for session in range(1, args.sessions + 1):
        proxy_url = f"http://ualfuslo-fr-{session}:{WEBSHARE_PROXY_PASS}@{host}"
        print(f"=== session fr-{session} via {host} ===", flush=True)
        launch = {"headless": True, "locale": "fr-FR", "humanize": True,
                  "geoip": False, "proxy": {"server": proxy_url}}
        session_ok = False
        try:
            with Camoufox(**launch) as browser:
                ctx = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris",
                                          viewport={"width": 1440, "height": 900})
                page = ctx.new_page()

                # ---- page 1: verify the session loads REAL content ----
                ok = False
                for attempt in range(3):
                    try:
                        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
                        ok = True
                        break
                    except Exception as e:
                        print(f"  page 1 goto {attempt+1}: {type(e).__name__}", flush=True)
                        time.sleep(5)
                if not ok:
                    print("  page 1 failed — next session", flush=True)
                    continue
                page.wait_for_timeout(int(PAGE_SETTLE_S * 1000))
                for _ in range(SCROLL_PASSES):
                    page.mouse.wheel(0, 1500)
                    page.wait_for_timeout(int(SCROLL_WAIT_S * 1000))
                html = page.content()
                if len(html) < 3000 or any(b in html.lower() for b in
                                           ("geochallenge", "captcha-delivery", "prouvez")):
                    print(f"  session fr-{session} IP flagged (challenge) — next", flush=True)
                    continue
                session_ok = True

                # ---- click through pages ----
                for page_idx in range(1, args.max_pages + 1):
                    if page_idx > 1:
                        if not click_page(page, page_idx):
                            print(f"  page {page_idx}: no button — done", flush=True)
                            break
                        advanced = False
                        for _ in range(25):
                            page.wait_for_timeout(1000)
                            if current_page(page) == page_idx:
                                ncards = page.evaluate(
                                    """() => document.querySelectorAll('[data-testid^="classified-card-mfe-"]').length""")
                                if ncards > 0:
                                    advanced = True
                                    break
                        if not advanced:
                            # one retry: the nav re-rendered and the button moved
                            print(f"  page {page_idx}: state {current_page(page)}, retrying click...", flush=True)
                            page.wait_for_timeout(2000)
                            if not click_page(page, page_idx):
                                print(f"  page {page_idx}: retry failed — done", flush=True)
                                break
                            for _ in range(15):
                                page.wait_for_timeout(1000)
                                if current_page(page) == page_idx:
                                    ncards = page.evaluate(
                                        """() => document.querySelectorAll('[data-testid^="classified-card-mfe-"]').length""")
                                    if ncards > 0:
                                        advanced = True
                                        break
                            if not advanced:
                                print(f"  page {page_idx}: stuck after retry — done", flush=True)
                                break

                    page.wait_for_timeout(int(PAGE_SETTLE_S * 1000))
                    for _ in range(SCROLL_PASSES):
                        page.mouse.wheel(0, 1500)
                        page.wait_for_timeout(int(SCROLL_WAIT_S * 1000))

                    html = page.content()
                    (outdir / f"page_{page_idx:02d}.html").write_text(html, encoding="utf-8")
                    listings = parse_ufrn(html, args.url)
                    new = 0
                    for l in listings:
                        if l["url"] and l["url"] not in seen:
                            seen.add(l["url"])
                            all_listings.append(l)
                            new += 1
                    m = re.search(r'"totalCount":(\d+)', html)
                    if m and total_count is None:
                        total_count = int(m.group(1))
                    print(f"  page {page_idx}: {len(listings)} cards ({new} new) — total {len(all_listings)}", flush=True)
                    if len(listings) == 0:
                        print("  no cards — done", flush=True)
                        break
        except Exception as e:
            print(f"  session fr-{session} error: {type(e).__name__}: {str(e)[:60]}", flush=True)
        if session_ok and len(all_listings) > 0:
            break

    out = outdir / "all_listings_browser.json"
    out.write_text(json.dumps(all_listings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {len(all_listings)} unique listings → {out}")
    if total_count:
        print(f"SeLoger total: {total_count} annonces")

    try:
        from listings_db import ListingsDB
        db = ListingsDB(Path(__file__).parent / "listings.db")
        db.bulk_upsert(all_listings)
        print(f"DB upsert: {len(all_listings)} rows (total {db.count()})")
        db.close()
    except Exception as e:
        print(f"DB upsert failed: {type(e).__name__}: {str(e)[:80]}")


if __name__ == "__main__":
    main()
