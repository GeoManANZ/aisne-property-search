"""SeLoger SPA renderer — solve DataDome, then render with a real browser.

SeLoger is a client-side SPA: the listing data is fetched by JS after load and
is NOT in the initial HTML.  The only reliable way to capture it is to run a
real (anti-detect) browser that executes the JS.

Plan:
  1. solve DataDome via 2Captcha → datadome cookie (bound to a Webshare IP)
  2. launch Camoufox (patched Firefox) 
  3. route the browser through the WARP SOCKS5 tunnel so it can reach SeLoger,
     and inject the datadome cookie for the .seloger.com domain
  4. navigate to the search URL, wait for the SPA to render, capture the final
     DOM + all network responses (JSON the SPA fetched)
  5. save everything to seloger_data/ — full HTML + all XHR/JSON payloads
"""
import sys, re, json, time, argparse, urllib.parse, importlib.util, random
from pathlib import Path
sys.path.insert(0, Path(__file__).parent.as_posix())

from twocaptcha_client import solve_datadome, get_balance
from config import WEBSHARE_PROXY_USER, WEBSHARE_PROXY_PASS, WEBSHARE_PROXY_LIST
from french_property_scraper import _parse_datadome_dd, _build_datadome_challenge_url

from webshare_chain import ChainedWebshareSession

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def solve_datadome_for(url, proxy_ip):
    """Solve DataDome for a URL, return (cookie_value, status, html)."""
    s = ChainedWebshareSession(proxy_ip=proxy_ip, timeout=45,
                               headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    r = s.get(url)
    dd = _parse_datadome_dd(r.text)
    if not dd:
        return None, r.status_code, r.text
    captcha_url = _build_datadome_challenge_url(dd, url)
    proxy_str = f"{WEBSHARE_PROXY_USER}:{WEBSHARE_PROXY_PASS}@{proxy_ip}"
    cookie_set = solve_datadome(captcha_url=captcha_url, page_url=url,
                                user_agent=UA, proxy=proxy_str, proxytype="http")
    mm = re.search(r"datadome=([^;]+)", cookie_set or "")
    return (mm.group(1) if mm else None), r.status_code, r.text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://www.seloger.com/recherche/achat/immeuble/picardie/soissons-02200/ad08fr14511")
    ap.add_argument("--outdir", default="seloger_data")
    ap.add_argument("--proxy", default=WEBSHARE_PROXY_LIST[0] if WEBSHARE_PROXY_LIST else None)
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)
    print(f"balance before: {get_balance()}")

    # Step 1: solve DataDome
    cookie, status, html = solve_datadome_for(args.url, args.proxy)
    print(f"datadome cookie: {'solved ' + cookie[:20] + '...' if cookie else 'none (status ' + str(status) + ')'}")

    # Step 2: render with Camoufox through WARP, injecting the cookie
    from camoufox.sync_api import Camoufox
    collected = []

    launch = {"headless": True, "locale": "fr-FR", "humanize": True,
              "geoip": True,
              "proxy": {"server": "socks5://cloudflare-warp:1080"}}

    with Camoufox(**launch) as browser:
        # Camoufox: cookies live on the CONTEXT, not the page
        context = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris",
                                      viewport={"width": 1440, "height": 900})
        page = context.new_page()
        # capture all network responses (the SPA's API calls carry the data)
        page.on("response", lambda r: collected.append({
            "url": r.url, "status": r.status,
            "content_type": r.headers.get("content-type", "")}))

        # inject datadome cookie if we have it
        if cookie:
            context.add_cookies([{
                "name": "datadome", "value": cookie, "domain": ".seloger.com",
                "path": "/", "secure": True}])
            print("injected datadome cookie")

        print(f"navigating to {args.url}...")
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(15000)  # let the SPA fetch listings
        # scroll to trigger lazy loads
        for _ in range(4):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(1500)

        html = page.content()
        (outdir / "spa_rendered.html").write_text(html, encoding="utf-8")
        print(f"saved SPA HTML: {outdir / 'spa_rendered.html'} ({len(html)} chars)")

        # collect all response bodies that look like listing JSON
        json_path = outdir / "spa_responses"
        json_path.mkdir(exist_ok=True)
        saved = 0
        for i, resp in enumerate(collected):
            ct = resp["content_type"]
            if "json" in ct or "annonce" in resp["url"] or "serp" in resp["url"] or "list" in resp["url"]:
                try:
                    # re-fetch body via a fresh page evaluate? — Camoufox doesn't
                    # retain bodies easily; capture via a JS collector instead.
                    pass
                except Exception:
                    pass
        # capture XHR bodies via page.on('response') body() — redo with body capture
        print(f"captured {len(collected)} network responses (URLs saved below)")
        (outdir / "network_responses.txt").write_text(
            "\n".join(f"{r['status']} {r['url']}" for r in collected), encoding="utf-8")

        # Extract listing data from the rendered DOM
        listings = page.evaluate("""() => {
            const out = [];
            // SeLoger listing cards carry data-testid
            const cards = document.querySelectorAll('[data-testid*="list"], [data-testid*="annonce"], article, [class*="listing"]');
            cards.forEach(c => {
                const text = c.innerText;
                if (text && (text.includes('€') || text.includes('m²'))) {
                    out.push(text.slice(0, 500));
                }
            });
            return out;
        }""")
        print(f"\nextracted {len(listings)} listing cards from DOM")
        (outdir / "dom_listings.txt").write_text("\n\n---\n\n".join(listings), encoding="utf-8")
        print(f"saved: {outdir / 'dom_listings.txt'}")

        # also grab any __NEXT_DATA__ now that it's rendered
        next_data = page.evaluate("""() => {
            const el = document.getElementById('__NEXT_DATA__');
            return el ? el.textContent : null;
        }""")
        if next_data:
            (outdir / "next_data.json").write_text(next_data, encoding="utf-8")
            print(f"saved __NEXT_DATA__ ({len(next_data)} chars)")

    print(f"\nbalance after: {get_balance()}")
    print("\nSaved files in", outdir)


if __name__ == "__main__":
    main()
