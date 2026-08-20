"""SeLoger search scraper — solve DataDome, capture HTML, parse embedded JSON.

SeLoger serves listings as a JS SPA: the search results data is embedded in the
page as JSON (Next.js / Apollo state or __NEXT_DATA__).  This script:
  1. fetches a SeLoger search URL through the WARP→Webshare chain,
  2. solves DataDome via 2Captcha if challenged,
  3. saves the full solved HTML to disk (all data preserved),
  4. parses the embedded JSON into clean listing records and prints them.

All retrieved data is saved — nothing useful is dropped.
"""
import sys, re, json, time, argparse, urllib.parse
from pathlib import Path
sys.path.insert(0, Path(__file__).parent.as_posix())

from webshare_chain import ChainedWebshareSession
from twocaptcha_client import solve_datadome, get_balance
from config import (
    WEBSHARE_PROXY_USER, WEBSHARE_PROXY_PASS, WEBSHARE_PROXY_LIST,
    build_fingerprint,
)
# DataDome helpers live in the scraper module
from french_property_scraper import (
    _parse_datadome_dd, _build_datadome_challenge_url,
)

UA = build_fingerprint()["user_agent"]


def fetch_with_datadome(url, proxy_ip, out_path: Path):
    """Fetch a SeLoger URL, solving DataDome if needed.  Returns (status, html)."""
    s = ChainedWebshareSession(proxy_ip=proxy_ip, timeout=45,
                               headers={"User-Agent": UA,
                                        "Accept-Language": "fr-FR,fr;q=0.9"})
    r = s.get(url)
    html = r.text
    dd = _parse_datadome_dd(html)
    if dd:
        print(f"  DataDome challenge detected (t={dd.get('t')}) — solving via 2Captcha...")
        captcha_url = _build_datadome_challenge_url(dd, url)
        proxy_str = f"{WEBSHARE_PROXY_USER}:{WEBSHARE_PROXY_PASS}@{proxy_ip}"
        cookie_set = solve_datadome(captcha_url=captcha_url, page_url=url,
                                    user_agent=UA, proxy=proxy_str, proxytype="http")
        mm = re.search(r"datadome=([^;]+)", cookie_set or "")
        if not mm:
            print("  !! DataDome solve failed")
            return r.status_code, html
        s2 = ChainedWebshareSession(proxy_ip=proxy_ip, timeout=45,
                                    headers={"User-Agent": UA,
                                             "Accept-Language": "fr-FR,fr;q=0.9",
                                             "Cookie": f"datadome={mm.group(1)}"})
        r2 = s2.get(url)
        html = r2.text
        print(f"  refetch after solve: status={r2.status_code}, len={len(html)}")
    # Save full HTML (all data preserved)
    out_path.write_text(html, encoding="utf-8")
    return r.status_code if 'r2' not in locals() else r2.status_code, html


def extract_json_blobs(html: str):
    """Yield all sizeable JSON objects embedded in the page."""
    # __NEXT_DATA__
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            yield json.loads(m.group(1))
        except Exception:
            pass
    # window.__INITIAL_STATE__ / __APOLLO_STATE__
    for pat in [r'window\.__INITIAL_STATE__\s*=\s*', r'window\.__APOLLO_STATE__\s*=\s*']:
        m = re.search(pat + r'(\{.*?\});?\s*</script>', html, re.S)
        if m:
            try:
                yield json.loads(m.group(1))
            except Exception:
                pass
    # any <script type="application/json">
    for m in re.finditer(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S):
        try:
            yield json.loads(m.group(1))
        except Exception:
            pass


def parse_listings(data, results: list):
    """Recursively walk a JSON blob looking for SeLoger listing objects."""
    if isinstance(data, dict):
        # SeLoger listing shape: has id + title + price(s) + propertyType
        keys = set(data.keys())
        if "title" in data and ("price" in data or "pricing" in data) and "propertyType" in data:
            results.append(data)
        for v in data.values():
            parse_listings(v, results)
    elif isinstance(data, list):
        for v in data:
            parse_listings(v, results)


def extract_links(html: str):
    """Collect all listing detail URLs found in the page (nothing left behind)."""
    links = set()
    for m in re.finditer(r'href="(https://www\.seloger\.com/annonces/[^"]+)"', html):
        links.add(m.group(1))
    return links


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://www.seloger.com/recherche/achat/immeuble/departement-aisne-02/")
    ap.add_argument("--outdir", default="seloger_data")
    ap.add_argument("--proxy", default=WEBSHARE_PROXY_LIST[0] if WEBSHARE_PROXY_LIST else None)
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)

    print(f"balance before: {get_balance()}")
    print(f"URL: {args.url}")
    print(f"proxy: {args.proxy}")

    html_path = outdir / "search_solved.html"
    status, html = fetch_with_datadome(args.url, args.proxy, html_path)
    print(f"saved HTML: {html_path} ({len(html)} chars)")

    # Extract all JSON blobs
    blobs = list(extract_json_blobs(html))
    print(f"\nextracted {len(blobs)} JSON blobs")

    # Find listing objects
    listings = []
    for blob in blobs:
        parse_listings(blob, listings)
    print(f"found {len(listings)} listing objects")

    # Extract detail links too
    links = extract_links(html)
    print(f"found {len(links)} detail links")
    (outdir / "detail_links.txt").write_text("\n".join(sorted(links)), encoding="utf-8")
    print(f"saved detail links: {outdir / 'detail_links.txt'}")

    # Save JSON blobs for full data preservation
    blobs_path = outdir / "json_blobs.json"
    blobs_path.write_text(json.dumps(blobs, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved JSON blobs: {blobs_path}")

    # Print a sample listing if found
    if listings:
        print("\n=== sample listing ===")
        print(json.dumps(listings[0], ensure_ascii=False, indent=2)[:1500])

    print(f"\nbalance after: {get_balance()}")


if __name__ == "__main__":
    main()
