#!/usr/bin/env python3
"""Why does the SeLoger RENT pass keep returning nothing?

The cron log is ambiguous: 'page loaded 2 MB, page 1: 0 ids (total None)' is
either a BLOCK (the BFF refuses the API call) or an EMPTY-but-valid response
(the criteria match no rentals). Those need opposite fixes, and both have been
reported as 'still blocked' for days.

This settles it without a browser: reuse the already-earned datadome cookie and
POST the BFF directly. One call costs tens of KB instead of a 2 MB page load.

Three probes, printed side by side:
  A) Rent as the code builds it today  (inherits projectTypes New_Build/Resale)
  B) Rent WITHOUT projectTypes        (rentals have no resale/new-build concept)
  C) Buy  (control — if this is empty too, we are blocked)

A) empty + B) >0  => criteria bug, not a block.  All three empty/403 => blocked.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, Path(__file__).parent.as_posix())
import config
from cookie_store import get_cookies
from seloger_api_sweep import SELOGER_DOMAIN, build_criteria

URL = "https://www.seloger.com/serp-bff/search"


def probe(label: str, criteria: dict, proxy_url: str, cookies: list, ua: str):
    body = json.dumps({"criteria": criteria,
                       "paging": {"page": 1, "size": 30, "order": "Default"}},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", ua)
    req.add_header("Origin", "https://www.seloger.com")
    req.add_header("Referer", "https://www.seloger.com/")
    if cookies:
        req.add_header("Cookie", "; ".join(f"{c['name']}={c['value']}" for c in cookies))

    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(req, timeout=45) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except Exception as e:
        print(f"  {label:38s} -> {type(e).__name__}: {str(e)[:70]}")
        return

    text = raw.decode("utf-8", "replace")
    try:
        d = json.loads(text)
        total = d.get("totalCount")
        n = len(d.get("classifieds") or [])
        print(f"  {label:38s} -> HTTP {status}  totalCount={total}  ids={n}")
        if not n:
            print(f"      body head: {text[:180]}")
    except Exception:
        print(f"  {label:38s} -> HTTP {status}  NON-JSON ({len(raw)} bytes)")
        print(f"      body head: {text[:180]}")


def main():
    session = 1
    key = f"{config.WEBSHARE_FR_STICKY_PREFIX}-{session}"
    proxy_url = config.webshare_rotate_url(session=session, country="fr", host_idx=0)
    cookies = get_cookies(key, SELOGER_DOMAIN)
    ua = config.build_fingerprint()["user_agent"]
    names = [c["name"] for c in cookies]
    print(f"cookie store key: {key}   cookies: {names or 'NONE'}")
    print(f"proxy: {proxy_url.split('@')[-1] if '@' in proxy_url else proxy_url}")
    print()

    rent_as_is = build_criteria(estate_type="Apartment", distribution_type="Rent")
    rent_no_project = dict(rent_as_is)
    rent_no_project.pop("projectTypes", None)
    buy = build_criteria(estate_type="Apartment", distribution_type="Buy")

    print("probe A) Rent — exactly as the code builds it today")
    probe("A Rent (with projectTypes)", rent_as_is, proxy_url, cookies, ua)
    print("probe B) Rent — same but WITHOUT projectTypes")
    probe("B Rent (no projectTypes)", rent_no_project, proxy_url, cookies, ua)
    print("probe C) Buy — control")
    probe("C Buy (control)", buy, proxy_url, cookies, ua)


if __name__ == "__main__":
    main()
