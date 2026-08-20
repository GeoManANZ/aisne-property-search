"""Test: DataDome solve on SeLoger via 2Captcha — full flow with challenge URL.

Builds the geo.captcha-delivery.com challenge URL from the dd object fields
served in the DataDome 403 body, solves via 2Captcha (with the matching
WARP-chained proxy), then re-fetches with the datadome cookie.

ONE 2Captcha solve (~$0.003).
"""
import sys, re, json, urllib.parse
sys.path.insert(0, "/workspace/hermes1/projects/aisne-property-search")

URL = "https://www.seloger.com/annonces/achat/immeuble/aisne-02/"
PROXY_IP = "31.59.20.176:6754"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

from webshare_chain import ChainedWebshareSession
from twocaptcha_client import solve_datadome, get_balance


def _parse_dd(body: str) -> dict:
    """Extract the DataDome `var dd={...}` object from the challenge page."""
    start = body.find("var dd=")
    if start == -1:
        return {}
    i = body.find("{", start)
    depth = 0
    for j in range(i, len(body)):
        if body[j] == "{":
            depth += 1
        elif body[j] == "}":
            depth -= 1
            if depth == 0:
                raw = body[i:j + 1]
                # single-quote → double-quote JSON
                raw = raw.replace("'", '"')
                try:
                    return json.loads(raw)
                except Exception:
                    # fall back to key:value regex
                    return dict(re.findall(r'"(\w+)":"([^"]*)"', raw))
    return {}


print("balance before:", get_balance())
s = ChainedWebshareSession(proxy_ip=PROXY_IP, timeout=40,
                           headers={"User-Agent": UA,
                                    "Accept-Language": "fr-FR,fr;q=0.9"})

# Step 1: fetch → get the dd object with challenge fields
r = s.get(URL)
dd = _parse_dd(r.text)
print("dd fields:", {k: dd.get(k) for k in ["rt", "cid", "hsh", "t", "s", "e", "host"]})

if not dd:
    print("no dd object — not a DataDome challenge?")
    sys.exit(1)

# Step 2: build the challenge URL
cid = dd.get("cid", "")
hsh = dd.get("hsh", "")
t = dd.get("t", "fe")
s_val = dd.get("s", "")
e_val = dd.get("e", "")
host = dd.get("host", "geo.captcha-delivery.com")
referer = urllib.parse.quote(URL, safe="")
captcha_url = (f"https://{host}/captcha/?initialCid={urllib.parse.quote(cid)}"
               f"&hash={hsh}&cid={urllib.parse.quote(cid)}&t={t}"
               f"&referer={referer}&s={s_val}&e={e_val}")
print("captcha_url:", captcha_url[:120], "...")

# Step 3: solve via 2Captcha with matching proxy
proxy_str = f"ualfuslo:ukzubke2lnit@{PROXY_IP}"
cookie_set = solve_datadome(captcha_url=captcha_url, page_url=URL,
                            user_agent=UA, proxy=proxy_str, proxytype="http")
print("2captcha result:", (cookie_set[:70] + "...") if cookie_set else "FAILED")

if cookie_set and not cookie_set.startswith("ERROR"):
    mm = re.search(r"datadome=([^;]+)", cookie_set)
    if mm:
        datadome_val = mm.group(1)
        print("datadome cookie:", datadome_val[:40], "...")
        # Step 4: re-fetch with the solved cookie
        s2 = ChainedWebshareSession(proxy_ip=PROXY_IP, timeout=40,
                                    headers={"User-Agent": UA,
                                             "Accept-Language": "fr-FR,fr;q=0.9",
                                             "Cookie": f"datadome={datadome_val}"})
        r2 = s2.get(URL)
        print(f"refetch with solved cookie: status={r2.status_code}, len={len(r2.text)}")
        print("has listing:", "annonce" in r2.text.lower() or "immeuble" in r2.text.lower())
        if r2.status_code in (200, 404) and len(r2.text) > 3000 and "annonce" in r2.text.lower():
            open("seloger_solved.html", "w", encoding="utf-8").write(r2.text)
            print("✅ SAVED seloger_solved.html (status", r2.status_code, ")")
print("balance after:", get_balance())
