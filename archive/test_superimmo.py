"""Test: automate the Superimmo hCaptcha checkbox click.

Superimmo gates its listing pages behind hCaptcha (sitekey 12df5a02-...).
The hCaptcha iframe contains a checkbox.  With stealth Chromium + a clean
fingerprint the checkbox often passes the risk check without an image
challenge.  This engine:
  1. loads the page
  2. finds the hCaptcha iframe (hcaptcha.com/captcha)
  3. clicks the checkbox inside it
  4. waits for either the page to reveal content, or an image challenge

If an image-grid challenge appears, that's where vision analysis could be
plugged in (hCaptcha image puzzles ARE vision-solvable).
"""
import os, re, time
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/data/.playwright-browsers"
CHROMIUM = "/opt/hermes/.playwright/chromium-1228/chrome-linux64/chrome"
URL = "https://www.superimmo.com/vente/immeuble/aisne-02/"

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, executable_path=CHROMIUM,
                          args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    c = b.new_context(locale="fr-FR", timezone_id="Europe/Paris", viewport={"width": 1366, "height": 900})
    pg = c.new_page()
    Stealth().apply_stealth_sync(pg)
    pg.goto(URL, wait_until="domcontentloaded", timeout=40000)
    pg.wait_for_timeout(5000)

    print("Title:", pg.title())

    # Locate the hCaptcha iframe
    hcap = None
    for f in pg.frames:
        if "hcaptcha.com/captcha" in (f.url or ""):
            hcap = f
            print("hCaptcha frame found:", f.url[:70])
            break
    if not hcap:
        # try to find any hcaptcha frame
        for f in pg.frames:
            if "hcaptcha" in (f.url or ""):
                hcap = f; print("Alt hCaptcha frame:", f.url[:70]); break

    if hcap:
        # hCaptcha checkbox has aria-label / id 'checkbox'
        try:
            box = hcap.locator("#checkbox, [id=checkbox], [aria-label*='challenge'], .checkbox")
            print("Checkbox count:", box.count())
            if box.count():
                box.first.click(timeout=8000)
                print("✅ Clicked hCaptcha checkbox")
        except Exception as e:
            print("checkbox click err:", str(e)[:80])
            # try generic click on the frame body
            try:
                hcap.locator("body").click(position={"x": 20, "y": 20}, timeout=5000)
                print("  clicked frame body (20,20)")
            except Exception as e2:
                print("  frame click err:", str(e2)[:80])

        # Wait to see if challenge appears or content loads
        for i in range(10):
            time.sleep(2)
            title = pg.title()
            size = len(pg.content())
            # check for image challenge indicators in hcaptcha frame
            challenge = False
            if hcap and not hcap.is_closed():
                try:
                    ch = hcap.locator(".challenge-container, [class*='challenge'], .task")
                    if ch.count(): challenge = True
                except: pass
            print(f"  [{i+1}] title='{title[:40]}' size={size} challenge={'YES' if challenge else 'no'}")
            if "humain" not in title and size > 20000:
                print("\n✅ SUPERIMPO PAGE LOADED — content revealed")
                break
            if challenge:
                print("\n⚠ Image challenge presented — THIS is where vision plugs in")
                break
    else:
        print("No hCaptcha frame found — page may already be accessible")
        print("Body:", " ".join(pg.inner_text('body').split())[:200])
    b.close()
