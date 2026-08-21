"""
Superimmo hCaptcha vision solver — STANDALONE (no agent in the loop).

This is the FREE path for Superimmo: instead of paying a CAPTCHA service,
we drive a stealth browser, trigger the hCaptcha image-grid challenge, and
solve it ourselves by asking a vision model which tiles contain the target
animals, then clicking them.

Loop (with retries — the challenge rotates content each attempt):
  1. load Superimmo, click the hCaptcha checkbox
  2. screenshot the challenge iframe
  3. call the vision model → returns which grid cells hold targets
  4. click those cells (mapped to screen coords via grid geometry)
  5. submit; if the challenge persists, retry with a fresh vision read

╔══════════════════════════════════════════════════════════════════════╗
║  COST WARNING — THIS IS NOT A FREE API.                              ║
║  The vision model (MiMo / xiaomi mimo-v2.5) is a paid API.          ║
║  Use it CONSERVATIVELY: strictly on-demand, only when a CAPTCHA     ║
║  challenge is actually present, and never in a tight/hot loop.      ║
║  Each solve = 1–3 vision calls. Keep batch runs spaced out.         ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os
import base64
import json
import time
import sys

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/data/.playwright-browsers"
CHROMIUM = "/opt/hermes/.playwright/chromium-1228/chrome-linux64/chrome"

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# ---- Vision API config (MiMo, paid — see COST WARNING above) ---------------
VISION_BASE = "https://token-plan-sgp.xiaomimimo.com/v1"
VISION_MODEL = "mimo-v2.5"          # NOT -pro (that 404s on images)
VISION_API_KEY = (os.environ.get("HERMES1_XIAOMI_API_KEY")
                  or os.environ.get("XIAOMI_TTS_API_KEY") or "")

# ---- Superimmo + hCaptcha challenge geometry --------------------------------
URL = "https://www.superimmo.com/vente/immeuble/aisne-02/"
# The challenge iframe position is detected at runtime (it varies between
# instances); we no longer hardcode it.  The grid fills most of the iframe
# below the sidebar, so we ask vision for the tile grid location per-instance.

PROMPT = (
    "This is an hCaptcha image challenge. There is a grid of animal tiles "
    "(3 columns, 4 rows). The sidebar lists target animals to find. "
    "Respond ONLY with JSON: {\"grid\": {\"x\": <px>, \"y\": <px>, \"w\": <px>, "
    "\"h\": <px>}, \"cols\": 3, \"rows\": 4, \"targets\": [[row,col], ...]} "
    "where x/y/w/h is the pixel bounding box of the tile grid (top-left, "
    "width, height) measured in this image, and targets are 1-indexed "
    "row/col of every tile that should be clicked. If you cannot confidently "
    "identify any target tile, return {\"targets\": []} (but still give the "
    "grid bbox)."
)


def _cell_center(g, col: int, row: int) -> tuple[float, float]:
    """Map grid cell (row,col) to absolute page pixel coordinates.

    g = grid dict with x/y/w/h (bbox within the full-page screenshot) and
    cols/rows.  We ALSO need the challenge iframe's offset on the page so the
    screenshot coords translate to real mouse coords; the iframe offset is
    captured at runtime and folded in by the caller.
    """
    cw = g["w"] / g["cols"]
    ch = g["h"] / g["rows"]
    cx = g["x"] + (col - 1) * cw + cw / 2
    cy = g["y"] + (row - 1) * ch + ch / 2
    return cx, cy


def _vision_identify(screenshot_path: str) -> dict:
    """Ask the vision model for the grid geometry + target cells.  Paid API —
    call only when a challenge is actually on screen.  Returns a dict with
    keys: grid (bbox dict), cols, rows, targets (list of [row,col])."""
    if not VISION_API_KEY:
        print("  ! no vision API key set — cannot solve via vision")
        return {"targets": []}
    with open(screenshot_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    import urllib.request
    body = json.dumps({
        "model": VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": PROMPT},
            ],
        }],
        "max_tokens": 300,
    }).encode()
    req = urllib.request.Request(
        f"{VISION_BASE}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {VISION_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        text = data["choices"][0]["message"]["content"]
        # extract JSON from the model reply
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return {"targets": []}
        parsed = json.loads(text[start:end + 1])
        return parsed
    except Exception as e:
        print(f"  ! vision call failed: {type(e).__name__}: {str(e)[:100]}")
        return {"targets": []}


def solve_superimmo(url: str = URL, max_attempts: int = 3) -> str:
    """Return the listing HTML after solving the hCaptcha, or '' on failure.

    max_attempts: vision solves are ~90% accurate, so we retry up to this
    many times.  Each retry = 1 paid vision call; keep it small.
    """
    with sync_playwright() as p:
        b = p.chromium.launch(
            headless=True, executable_path=CHROMIUM,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        c = b.new_context(locale="fr-FR", timezone_id="Europe/Paris",
                          viewport={"width": 1366, "height": 900})
        pg = c.new_page()
        Stealth().apply_stealth_sync(pg)
        pg.goto(url, wait_until="domcontentloaded", timeout=40000)
        pg.wait_for_timeout(8000)

        # Click the hCaptcha checkbox (triggers the image challenge)
        el = pg.query_selector(".h-captcha")
        if not el:
            # maybe already past it — check content
            html = pg.content()
            if "humain" not in html.lower():
                return html
            print("  ! no hCaptcha widget found")
            b.close()
            return ""
        bb = el.bounding_box()
        pg.mouse.click(bb["x"] + 25, bb["y"] + bb["height"] / 2)
        pg.wait_for_timeout(5000)

        for attempt in range(max_attempts):
            pg.screenshot(path="/tmp/hcap_solve.png")
            vres = _vision_identify("/tmp/hcap_solve.png")
            targets = vres.get("targets", [])
            grid = vres.get("grid") or {}
            print(f"  attempt {attempt+1}: vision grid={grid} targets={targets}")
            if not targets or not grid.get("w"):
                time.sleep(3)
                continue

            # Build a grid spec with the runtime bbox (already in viewport
            # px, matching mouse coords since screenshot == viewport).
            gspec = {"x": grid["x"], "y": grid["y"],
                     "w": grid["w"], "h": grid["h"],
                     "cols": vres.get("cols", 3), "rows": vres.get("rows", 4)}

            # Click each target cell (small-step moves for natural feel)
            for col, row in targets:
                x, y = _cell_center(gspec, int(col), int(row))
                pg.mouse.move(x, y, steps=6)
                time.sleep(0.2)
                pg.mouse.click(x, y)
                time.sleep(0.4)

            # Submit — hCaptcha auto-submits after selection, or click Valider
            time.sleep(2)
            html = pg.content()
            if "humain" not in html.lower():
                b.close()
                return html  # solved!
            # try the Valider button if still there
            valider = pg.query_selector("button[type=submit], .button-submit, button:has-text('Valider')")
            if valider:
                try:
                    valider.click()
                    pg.wait_for_timeout(3000)
                except Exception:
                    pass
            html = pg.content()
            if "humain" not in html.lower():
                b.close()
                return html

        b.close()
        return ""


if __name__ == "__main__":
    print("Solving Superimmo hCaptcha via vision...")
    html = solve_superimmo()
    if html:
        print(f"SOLVED — got {len(html)} chars of listing HTML")
        with open("superimmo_solved.html", "w", encoding="utf-8") as f:
            f.write(html)
    else:
        print("FAILED to solve Superimmo via vision")
