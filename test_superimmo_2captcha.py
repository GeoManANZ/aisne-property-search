"""Test: solve Superimmo hCaptcha via 2Captcha and inject the token.

Uses ONE 2Captcha solve (~$0.003-0.01).  hCaptcha tokens work from 2Captcha's
own infrastructure — no proxy needed (unlike DataDome).  The solved token is
injected into the page's hidden hcaptcha input + we trigger the form submit.
"""
import os, sys, time, json
sys.path.insert(0, "/workspace/hermes1/projects/aisne-property-search")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/data/.playwright-browsers"
CHROMIUM = "/opt/hermes/.playwright/chromium-1228/chrome-linux64/chrome"

from twocaptcha_client import solve_hcaptcha, get_balance
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

SITEKEY = "12df5a02-1e3b-4b8e-8d64-ef72a6eb3784"
URL = "https://www.superimmo.com/achat/immeuble/picardie/aisne/soissons-02200"

print("balance:", get_balance())
print("solving hcaptcha via 2captcha...")
token = solve_hcaptcha(SITEKEY, URL)
print("token:", (token[:40] + "...") if token and not token.startswith("ERROR") else token)
if not token or token.startswith("ERROR"):
    print("FAILED to get token")
    sys.exit(1)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, executable_path=CHROMIUM,
                          args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    c = b.new_context(locale="fr-FR", timezone_id="Europe/Paris", viewport={"width":1366,"height":900})
    pg = c.new_page(); Stealth().apply_stealth_sync(pg)
    pg.goto(URL, wait_until="domcontentloaded", timeout=40000); pg.wait_for_timeout(8000)
    print("title before:", pg.title())

    # Inject the solved token into the hcaptcha hidden input and trigger submit
    try:
        pg.evaluate("""(token) => {
            const inp = document.querySelector('[name="h-captcha-response"], #h-captcha-response');
            if (inp) inp.value = token;
            // trigger hCaptcha success callback if defined
            if (window.hcaptcha && window.hcaptcha.setResponse) {
                try { window.hcaptcha.setResponse(token); } catch(e) {}
            }
        }""", token)
        print("injected token")
    except Exception as e:
        print("inject err:", str(e)[:80])

    pg.wait_for_timeout(3000)
    # Correct flow: DON'T submit the interstitial form (that navigates to a
    # 404).  Instead re-navigate to the ORIGINAL target URL — the solved token
    # lets the site recognise us as human and serve the real listings page.
    try:
        pg.goto(URL, wait_until="domcontentloaded", timeout=40000)
        pg.wait_for_timeout(8000)
    except Exception:
        pass
    print("title after:", pg.title())
    html = pg.content()
    print("len after:", len(html))
    print("past captcha:", "humain" not in html.lower())
    if "humain" not in html.lower() and len(html) > 3000:
        open("superimmo_solved.html","w",encoding="utf-8").write(html)
        print("saved superimmo_solved.html")
    b.close()
