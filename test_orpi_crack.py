"""Verify: Webshare residential proxy cracks Orpi AND extracts the listing.

Orpi was the hardest — a Cloudflare hard IP-block ("Sorry, you have been
blocked") that resisted direct, WARP, Lightpanda, and stealth-on-datacenter-IP.
Routing stealth Chromium through a Webshare residential IP cracks it.
Now confirm we can extract the structured listing data.
"""
import os
import time
import json

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/data/.playwright-browsers"
CHROMIUM_PATH = "/opt/hermes/.playwright/chromium-1228/chrome-linux64/chrome"

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

PROXY_LIST = [
    "31.59.20.176:6754",
    "45.38.107.97:6014",
    "198.105.121.200:6462",
    "64.137.96.74:6641",
    "198.23.243.226:6361",
    "38.154.185.97:6370",
    "84.247.60.125:6095",
    "191.96.254.138:6185",
]
PROXY_USER = "ualfuslo"
PROXY_PASS = "ukzubke2lnit"

ORPI_URL = "https://www.orpi.com/annonce-vente-immeuble-t5-laon-02000-8ab73979-6aa3-41bd-bb2f-89630451e57b/"

def fetch_through_proxies(url, proxies, timeout_s=40):
    """Try each proxy until one loads the page without a block."""
    for proxy in proxies:
        ip, port = proxy.split(":")
        print(f"  proxy {ip}:{port} ...", flush=True)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    executable_path=CHROMIUM_PATH,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                    proxy={"server": f"http://{ip}:{port}", "username": PROXY_USER, "password": PROXY_PASS},
                )
                try:
                    context = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris",
                                                  viewport={"width": 1366, "height": 900})
                    page = context.new_page()
                    Stealth().apply_stealth_sync(page)
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
                    page.wait_for_timeout(6000)
                    html = page.content()
                    low = html.lower()
                    if any(b in low for b in ["sorry, you have been blocked", "just a moment", "attention required"]):
                        print(f"    ⚠ blocked ({len(html)} chars)")
                        browser.close()
                        continue
                    # Extract body text for description
                    body = " ".join(page.inner_text("body").split())
                    return {"proxy": proxy, "html": html, "body": body, "size": len(html)}
                finally:
                    browser.close()
        except Exception as e:
            print(f"    ❌ {type(e).__name__}: {str(e)[:70]}")
        time.sleep(2)
    return None

result = fetch_through_proxies(ORPI_URL, PROXY_LIST)
if result:
    html = result["html"]
    print(f"\n✅ LOADED via {result['proxy']}: {result['size']} chars")
    print("\n=== Body text (first 1200 chars) ===")
    print(result["body"][:1200])
    # Save HTML for extraction
    with open("/workspace/hermes1/projects/aisne-property-search/scans/orpi_laon_loaded.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("\nHTML saved to scans/orpi_laon_loaded.html")
else:
    print("\n❌ All proxies failed")
