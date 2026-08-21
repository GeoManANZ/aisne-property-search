"""SeLoger full pagination via pure HTTP — the CORRECT architecture.

Research (2026-08-20): SeLoger paginates with `?LISTING-LISTpg=N` and embeds
ALL card data as JSON in the initial HTML (`window["__UFRN_FETCHER__"]`).
No browser, no clicking. One DataDome solve → one sticky French IP → all
pages via plain HTTP.

Flow:
  1. Solve DataDome once through RotatingWebshareSession(country='fr', session=N)
     (SOCKS5 proxytype; UA normalized to Windows Chrome; cycle sessions until
     one solves AND a refetch returns real listings)
  2. For page in 1..max_pages: GET url + "?LISTING-LISTpg={page}" through the
     SAME session + cookie; parse_ufrn() extracts the cards; save HTML + JSON.
  3. Upsert all listings into listings.db.

Usage:
  python seloger_paginate_http.py [--max-pages 9] [--sessions 12]
"""
import sys, re, time, json, argparse
from pathlib import Path
sys.path.insert(0, Path(__file__).parent.as_posix())

from twocaptcha_client import solve_datadome, get_balance
from config import build_fingerprint
from french_property_scraper import _parse_datadome_dd, _build_datadome_challenge_url
from french_property_parsers import parse_ufrn
from webshare_chain import RotatingWebshareSession

UA = build_fingerprint()["user_agent"]
BASE_URL = ("https://www.seloger.com/recherche/achat/immeuble/"
            "hauts-de-france/aisne-02/ad06fr2")


def solve_and_verify(session, url, country="fr"):
    """Solve DataDome for one session.  Returns (cookie, html) or (None, None)."""
    s = RotatingWebshareSession(session=session, timeout=45, country=country,
                                headers={"User-Agent": UA,
                                         "Accept-Language": "fr-FR,fr;q=0.9"})
    r = s.get(url)
    dd = _parse_datadome_dd(r.text)
    if not dd:
        return None, None
    cookie_set = solve_datadome(captcha_url=_build_datadome_challenge_url(dd, url),
                                page_url=url, user_agent=UA,
                                proxy=s.proxy_auth_str(), proxytype="socks5")
    mm = re.search(r"datadome=([^;]+)", cookie_set or "")
    if not mm:
        return None, None
    # verify the cookie works on the SAME session (IP-bound)
    s2 = RotatingWebshareSession(session=session, timeout=45, country=country,
                                 headers={"User-Agent": UA,
                                          "Accept-Language": "fr-FR,fr;q=0.9",
                                          "Cookie": f"datadome={mm.group(1)}"})
    r2 = s2.get(url)
    ok = "annonce" in r2.text.lower() and len(r2.text) > 3000
    return (mm.group(1), r2.text) if ok else (None, None)


def fetch_page(session, cookie, url, country="fr"):
    """GET one page carrying the solved cookie through the same session."""
    s = RotatingWebshareSession(session=session, timeout=45, country=country,
                                headers={"User-Agent": UA,
                                         "Accept-Language": "fr-FR,fr;q=0.9",
                                         "Cookie": f"datadome={cookie}"})
    return s.get(url)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=BASE_URL)
    ap.add_argument("--outdir", default="seloger_pages")
    ap.add_argument("--max-pages", type=int, default=9)
    ap.add_argument("--sessions", type=int, default=12,
                    help="French sessions to try (fr-1..fr-N)")
    ap.add_argument("--country", default="fr")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)
    print(f"balance before: {get_balance()}")
    print(f"target: {args.url}")

    # Phase 1: find a session that solves AND verifies
    cookie, session = None, None
    for sess in range(1, args.sessions + 1):
        print(f"trying fr-{sess} ...", flush=True)
        c, html = solve_and_verify(sess, args.url, args.country)
        if c:
            cookie, session = c, sess
            print(f"  ✓ fr-{sess} solved + verified ({len(html)} chars)", flush=True)
            break
        print(f"  ✗ fr-{sess}", flush=True)
    if not cookie:
        print("NO SESSION SOLVED — aborting (2Captcha may be rate-limiting)")
        sys.exit(1)

    # Phase 2: paginate with LISTING-LISTpg
    all_listings, seen = [], set()
    total_count = None
    for page in range(1, args.max_pages + 1):
        page_url = args.url + (f"?LISTING-LISTpg={page}" if page > 1 else "")
        try:
            r = fetch_page(session, cookie, page_url, args.country)
        except Exception as e:
            print(f"  page {page}: FETCH ERROR {type(e).__name__}: {str(e)[:60]}")
            break
        if r.status_code != 200 or len(r.text) < 3000:
            print(f"  page {page}: bad response status={r.status_code} len={len(r.text)}")
            break
        html = r.text
        (outdir / f"page_{page:02d}.html").write_text(html, encoding="utf-8")
        listings = parse_ufrn(html, page_url)
        new = 0
        for l in listings:
            if l["url"] and l["url"] not in seen:
                seen.add(l["url"])
                all_listings.append(l)
                new += 1
        # grab totalCount from pageProps if visible
        m = re.search(r'"totalCount":(\d+)', html)
        if m and total_count is None:
            total_count = int(m.group(1))
        print(f"  page {page}: {len(listings)} cards ({new} new) — total {len(all_listings)}", flush=True)
        # stop early if a page returns 0 new cards (end of results)
        if len(listings) == 0:
            print("  no cards — last page reached")
            break

    # Phase 3: persist
    out = outdir / "all_listings_ufrn.json"
    out.write_text(json.dumps(all_listings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {len(all_listings)} unique listings → {out}")
    if total_count:
        print(f"SeLoger reported total: {total_count} annonces")
    print(f"balance after: {get_balance()}")

    # upsert into DB
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
