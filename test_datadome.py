"""Test: full DataDome bypass on SeLoger via 2Captcha.

Flow:
  1. fetch SeLoger URL through a Webshare proxy → DataDome redirects to a
     geo.captcha-delivery.com challenge URL
  2. submit that challenge URL to 2Captcha (with the SAME proxy + UA)
  3. get back the datadome cookie
  4. re-fetch SeLoger with the datadome cookie → real listing HTML

Uses ONE 2Captcha solve (~$0.003).  Conservative — single call.
"""
import os, sys, json, time
sys.path.insert(0, "/workspace/hermes1/projects/aisne-property-search")

WEBSHARE_USER = "ualfuslo"
WEBSHARE_PASS = "ukzubke2lnit"
PROXIES = [
    "31.59.20.176:6754", "45.38.107.97:6014", "198.105.121.200:6462",
    "64.137.96.74:6641", "198.23.243.226:6361", "38.154.185.97:6370",
    "84.247.60.125:6095", "191.96.254.138:6185",
]
URL = "https://www.seloger.com/annonces/achat/immeuble/aisne-02/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

import requests
from twocaptcha_client import solve_datadome, get_balance

print("balance before:", get_balance())
proxy_ip = PROXIES[0]
proxies = {"http": f"http://{WEBSHARE_USER}:{WEBSHARE_PASS}@{proxy_ip}",
           "https": f"http://{WEBSHARE_USER}:{WEBSHARE_PASS}@{proxy_ip}"}
s = requests.Session()
s.headers.update({"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})

# Step 1: fetch → capture the DataDome challenge redirect URL
captcha_url = None
r = s.get(URL, proxies=proxies, timeout=30, allow_redirects=True)
print(f"step1: status={r.status_code}, len={len(r.text)}")
# DataDome challenge URL is usually in the page or a redirect
low = r.text.lower()
if "geo.captcha-delivery.com" in r.url or "geo.captcha-delivery.com" in low:
    # extract challenge URL
    import re
    m = re.search(r"https://geo\.captcha-delivery\.com[^\"'\s)]+", r.url + " " + r.text)
    if m:
        captcha_url = m.group(0)
print("captcha_url:", captcha_url)

if not captcha_url:
    print("No DataDome challenge detected (maybe not blocked on this proxy)")
else:
    # Step 2: solve via 2Captcha with matching proxy + UA
    proxy_str = f"{WEBSHARE_USER}:{WEBSHARE_PASS}@{proxy_ip}"
    cookie_set = solve_datadome(captcha_url=captcha_url, page_url=URL,
                                user_agent=UA, proxy=proxy_str, proxytype="http")
    print("2captcha cookie_set:", cookie_set[:80] if cookie_set else "FAILED")

    if cookie_set and not cookie_set.startswith("ERROR"):
        # parse datadome=... from the set-cookie string
        import re as _re
        mm = _re.search(r"datadome=([^;]+)", cookie_set)
        if mm:
            datadome_val = mm.group(1)
            print("datadome cookie value:", datadome_val[:40], "...")
            # Step 3: re-fetch with the cookie
            s.cookies.set("datadome", datadome_val, domain=".seloger.com")
            r2 = s.get(URL, proxies=proxies, timeout=30)
            print(f"step3: status={r2.status_code}, len={len(r2.text)}")
            print("has listing:", "immeuble" in r2.text.lower() or "prix" in r2.text.lower())
            if len(r2.text) > 3000:
                open("seloger_solved.html", "w", encoding="utf-8").write(r2.text)
                print("saved seloger_solved.html")
    print("balance after:", get_balance())
