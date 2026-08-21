"""SeLoger BFF API scraper — the reliable way to get listing data.

SeLoger serves its search results via a client-side SPA that calls an internal
BFF (backend-for-frontend) JSON API.  The initial HTML is an empty shell
(<div id="root">), so there is nothing to parse server-side.  Instead we:

  1. solve DataDome via 2Captcha (through the WARP→Webshare chain) to get a
     valid `datadome` cookie bound to our egress IP,
  2. call SeLoger's internal search BFF endpoint directly with that cookie,
  3. save every response (full JSON + raw HTML) to disk so nothing is lost.

This is the same pattern the free French-eState-Scrapper repo uses (internal
BFF JSON APIs + a stealthy TLS client).  It avoids rendering the SPA entirely.
"""
import sys, re, json, time, argparse, urllib.parse, importlib.util
from pathlib import Path
sys.path.insert(0, Path(__file__).parent.as_posix())

from webshare_chain import ChainedWebshareSession
from twocaptcha_client import solve_datadome, get_balance
from config import WEBSHARE_PROXY_USER, WEBSHARE_PROXY_PASS, WEBSHARE_PROXY_LIST
from french_property_scraper import _parse_datadome_dd, _build_datadome_challenge_url

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def solve_datadome_for(url, proxy_ip):
    """Solve DataDome for a URL, return the datadome cookie value (or None)."""
    s = ChainedWebshareSession(proxy_ip=proxy_ip, timeout=45,
                               headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    r = s.get(url)
    dd = _parse_datadome_dd(r.text)
    if not dd:
        # maybe already allowed
        return None, r.status_code, r.text
    captcha_url = _build_datadome_challenge_url(dd, url)
    proxy_str = f"{WEBSHARE_PROXY_USER}:{WEBSHARE_PROXY_PASS}@{proxy_ip}"
    cookie_set = solve_datadome(captcha_url=captcha_url, page_url=url,
                                user_agent=UA, proxy=proxy_str, proxytype="http")
    mm = re.search(r"datadome=([^;]+)", cookie_set or "")
    return (mm.group(1) if mm else None), r.status_code, r.text


def call_bff(session, bff_path, query_params, extra_headers=None):
    """GET a BFF endpoint, return parsed JSON (or None on non-JSON/error)."""
    url = f"https://www.seloger.com{bff_path}"
    if query_params:
        url += "?" + urllib.parse.urlencode(query_params)
    hdrs = {"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.seloger.com/"}
    if extra_headers:
        hdrs.update(extra_headers)
    r = session.get(url)
    try:
        return r.status_code, json.loads(r.text)
    except Exception:
        return r.status_code, r.text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://www.seloger.com/recherche/achat/immeuble/departement-aisne-02/")
    ap.add_argument("--outdir", default="seloger_data")
    ap.add_argument("--proxy", default=WEBSHARE_PROXY_LIST[0] if WEBSHARE_PROXY_LIST else None)
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)
    print(f"balance before: {get_balance()}")
    print(f"proxy: {args.proxy}")

    # Step 1: solve DataDome
    cookie, status, html = solve_datadome_for(args.url, args.proxy)
    (outdir / "challenge_response.html").write_text(html, encoding="utf-8")
    if cookie:
        print(f"datadome solved: {cookie[:25]}...")
    else:
        print(f"no datadome solve needed (status {status}) — page may be shell or blocked")
        print("saved challenge_response.html")

    # Step 2: build an authenticated session (carries the cookie) and hit BFF
    if cookie:
        chain = ChainedWebshareSession(
            proxy_ip=args.proxy, timeout=45,
            headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9",
                     "Cookie": f"datadome={cookie}"})
    else:
        chain = ChainedWebshareSession(proxy_ip=args.proxy, timeout=45,
                                       headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})

    # Candidate BFF search endpoints for immeubles in Aisne (02)
    # department id for Aisne-02 on SeLoger; immeuble is a well-type.
    candidates = [
        ("/serp-bff/search", {"project": "2", "type": "3", "departmentCode": "02", "page": "1"}),
        ("/serp-bff/search", {"project": "2", "wellType": "immeuble", "departmentCode": "02", "page": "1"}),
        ("/recherche/api/annonces", {"project": "2", "type": "3", "places": "[{\"code\":\"02\"}]"}),
        ("/bff/api/v1/search", {"project": "2", "type": "3", "departmentCode": "02", "page": "1"}),
    ]
    saved_any = False
    for path, params in candidates:
        try:
            code, data = call_bff(chain, path, params)
            print(f"\nBFF {path}?{urllib.parse.urlencode(params)[:50]}... → {code} "
                  f"({len(json.dumps(data)[:100]) if isinstance(data, (dict, list)) else 'text'} chars)")
            if isinstance(data, (dict, list)) and data:
                fname = "bff_" + re.sub(r"[^a-z0-9]", "_", path.strip("/")) + ".json"
                (outdir / fname).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  saved {outdir / fname}")
                saved_any = True
        except Exception as e:
            print(f"BFF {path} error: {str(e)[:60]}")

    # Step 3: also save the fully-loaded search page via a rendered session
    (outdir / "search_shell.html").write_text(html, encoding="utf-8")

    print(f"\nbalance after: {get_balance()}")
    if not saved_any:
        print("\nNo BFF returned JSON. The endpoints may need exact params. "
              "Inspect challenge_response.html / bff_*.json and adjust.")


if __name__ == "__main__":
    main()
