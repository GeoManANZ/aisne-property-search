"""Test: Playwright stealth + Webshare residential IP against blocked portals.

The Webshare proxies are HTTP proxies (http://user:pass@ip:port), not SOCKS.
Routing Playwright Chromium through a residential IP gives us the one thing
that's been missing — a non-datacenter egress IP — which is what cracks:
  - Orpi       (Cloudflare hard IP-block)
  - SeLoger    (DataDome interactive CAPTCHA)
  - Logic-Immo (DataDome)
  - Superimmo  (click captcha)

We reuse the working proxy list from nz-mortgage-saas/scrapers/proxy_config.py.
"""
import os
import sys
import time
import random

# Point to an existing Chromium install (project only has Firefox)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/data/.playwright-browsers"
CHROMIUM_PATH = "/opt/hermes/.playwright/chromium-1228/chrome-linux64/chrome"

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# Webshare residential proxies (from nz-mortgage-saas/scrapers/proxy_config.py)
PROXY_LIST = [
    "31.59.20.176:6754",    # GB London
    "45.38.107.97:6014",    # GB London
    "198.105.121.200:6462", # GB London
    "64.137.96.74:6641",    # ES Madrid
    "198.23.243.226:6361",  # US LA
    "38.154.185.97:6370",   # US
    "84.247.60.125:6095",   # PL Warsaw
    "191.96.254.138:6185",  # US
]
PROXY_USER = "ualfuslo"
PROXY_PASS = "ukzubke2lnit"

SITES = [
    ("Orpi (hard IP block)", "https://www.orpi.com/annonce-vente-immeuble-t5-laon-02000-8ab73979-6aa3-41bd-bb2f-89630451e57b/"),
    ("SeLoger (DataDome)", "https://www.seloger.com/recherche/achat/immeuble/hauts-de-france/aisne-02/ad06fr2"),
    ("Logic-Immo (DataDome)", "https://www.logic-immo.com/recherche-immo/vente/immeuble/hauts-de-france/aisne-02/ad06fr2"),
]

BLOCK = ["just a moment", "attention required", "sorry, you have been blocked",
         "datadome", "captcha", "un instant", "prouvez que vous", "are you a robot"]

def try_proxy(url, proxy, timeout_s=35):
    """Try one URL through one webshare proxy with stealth Chromium."""
    ip, port = proxy.split(":")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=CHROMIUM_PATH,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            proxy={
                "server": f"http://{ip}:{port}",
                "username": PROXY_USER,
                "password": PROXY_PASS,
            },
        )
        try:
            context = browser.new_context(
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()
            Stealth().apply_stealth_sync(page)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            page.wait_for_timeout(6000)
            title = page.title()
            content = page.content()
            low = content.lower()
            hits = [b for b in BLOCK if b in low]
            if hits:
                return {"ok": False, "title": title[:50], "markers": hits, "size": len(content)}
            body = " ".join(page.inner_text("body").split())[:200]
            return {"ok": True, "title": title[:50], "body": body, "size": len(content)}
        finally:
            browser.close()

# Test each site with up to 3 proxies
for name, url in SITES:
    print(f"\n=== {name} ===")
    proxies_to_try = random.sample(PROXY_LIST, min(3, len(PROXY_LIST)))
    success = False
    for i, proxy in enumerate(proxies_to_try, 1):
        print(f"  [proxy {i}: {proxy}]", flush=True)
        try:
            result = try_proxy(url, proxy)
            if result["ok"]:
                print(f"    ✅ LOADED: {result['title']} ({result['size']} chars)")
                print(f"       body: {result['body']}")
                success = True
                break
            else:
                print(f"    ⚠ blocked: {result['title']} markers={result['markers']} ({result['size']} chars)")
        except Exception as e:
            print(f"    ❌ error: {type(e).__name__}: {str(e)[:80]}")
        time.sleep(2)  # gentle spacing
    if not success:
        print(f"    → FAILED through all {len(proxies_to_try)} proxies")
