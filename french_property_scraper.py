"""
FRENCH PROPERTY SCRAPER — Aisne (02) / Hauts-de-France
=======================================================

Multi-engine scraper with 4 cascading engines designed to defeat French
property-portal anti-bot systems (Cloudflare WAF, Imperva, JS challenges):

    Engine 1: direct    — plain HTTP with realistic browser headers.
                          Works for FNAIM, iad, ParuVendu, Notaires,
                          123webimmo, Superimmo (when not rate-limited).

    Engine 2: warp      — same HTTP but routed through the local Cloudflare
                          WARP SOCKS5 proxy (cloudflare-warp:1080).
                          Gives a residential-ish Cloudflare IP that avoids
                          datacenter-IP blacklists.  Helps when a portal
                          blocks the VPS IP range outright.

    Engine 3: lightpanda— headless browser via fastCRW MCP/Lightpanda.
                          Renders JavaScript, passes simple "Just a moment"
                          challenges.  Fails on aggressive WAFs that detect
                          the lightweight JS engine (e.g. Orpi).

    Engine 4: stealth   — FULL Chromium via Playwright + playwright-stealth.
                          The killer combo for Orpi / SeLoger / Logic-Immo /
                          Zilek / Superimmo.  Launches a real Chromium with
                          a patched fingerprint (WebGL, canvas, plugins,
                          navigator.*, CDP leaks) so Cloudflare's bot
                          detection sees a genuine browser.  Optionally
                          chained through WARP SOCKS5 for IP cleanliness.

The engines are tried in order ("cascade") and the first that returns a
non-blocked page wins.  Each result is saved as structured JSON plus raw
HTML for fallback parsing.

Usage:
    python3 french_property_scraper.py --url <URL> [--engine direct|warp|lightpanda|stealth|cascade]
    python3 french_property_scraper.py --input <file.txt> [--engine cascade]
    python3 french_property_scraper.py                       # demo run

Output layout (per batch, under scans/<scan_id>/):
    results.json          structured per-URL data (price, surface, DPE...)
    log.txt               per-engine diagnostics
    <name>.html           raw HTML of the best successful fetch

Dependencies:
    requests, PySocks, playwright, playwright-stealth
    (fastCRW for the lightpanda engine — http://fastcrw:3000)
"""

import json
import re
import sys
import time
import hashlib
import os
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict


# ---------------------------------------------------------------------------
# Standardised engine return type (review item 5)
# ---------------------------------------------------------------------------
@dataclass
class ScrapeResult:
    """Single canonical shape returned by every scraping engine.

    All engines (direct, warp, lightpanda, stealth, webshare*, camoufox,
    datadome) return this.  `scan_url` and downstream consumers read ONLY
    this shape.
    """
    success: bool
    html: str = ""
    status: int = 0
    engine: str = ""
    blocked: bool = False
    error: Optional[str] = None
    cookies: Optional[list] = None
    proxy_ip: Optional[str] = None
    timing_s: float = 0.0
    reused_cookie: bool = False
    rawHtml: str = ""          # alias so dict-style readers keep working

    def __post_init__(self):
        if self.rawHtml == "" and self.html:
            self.rawHtml = self.html
        elif self.html == "" and self.rawHtml:
            self.html = self.rawHtml

    def get(self, key: str, default=None):
        """dict-style access so existing dispatch code (crash_data.get(...))
        works unchanged whether an engine returns a dict or a ScrapeResult."""
        if key == "rawHtml":
            return self.rawHtml
        if key == "html":
            return self.html
        return getattr(self, key, default)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rawHtml"] = d["rawHtml"] or d["html"]
        return d

    @classmethod
    def from_dict(cls, d: dict, engine: str = "") -> "ScrapeResult":
        """Wrap an engine's raw dict return into a ScrapeResult."""
        return cls(
            success=bool(d.get("success", False)),
            html=d.get("rawHtml") or d.get("html", ""),
            status=int(d.get("status", 0)),
            engine=engine or d.get("engine", ""),
            blocked=bool(d.get("blocked", False)),
            error=d.get("error"),
            cookies=d.get("cookies"),
            proxy_ip=d.get("proxy_ip"),
            timing_s=float(d.get("timing_s", 0.0)),
            reused_cookie=bool(d.get("reused_cookie", False)),
        )


# ---------------------------------------------------------------------------
# Structured logging helper (review item 12)
# ---------------------------------------------------------------------------
import logging as _logging

_log = _logging.getLogger("aisne.scraper")
if not _log.handlers:
    _h = _logging.StreamHandler()
    _h.setFormatter(_logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _log.addHandler(_h)
    _log.setLevel(_logging.INFO)


def log_info(msg: str):
    _log.info(msg)


def log_error(msg: str):
    _log.error(msg)


def log_debug(msg: str):
    _log.debug(msg)


# ---------------------------------------------------------------------------
# CONFIG — single source of truth in config.py
# ---------------------------------------------------------------------------
from config import (
    WARP_HOST, WARP_PORT, WARP_SOCKS5, FASTCRW_URL, SCAN_DIR,
    BROWSER_HEADERS as _BROWSER_HEADERS,
    BLOCK_PHRASES as _BLOCK_PHRASES,
    WEBSHARE_PROXY_USER, WEBSHARE_PROXY_PASS, WEBSHARE_PROXY_LIST,
    CHROMIUM_PATH as _CHROMIUM_PATH,
    TWOCAPTCHA_API_KEY, DEFAULT_TIMEOUT_S, CAPTCHA_MAX_SPEND_USD,
)

_WARP_HOST = WARP_HOST
_WARP_PORT = WARP_PORT

# ---------------------------------------------------------------------------
# ENGINE 1 + 2 — HTTP with headers, optionally through WARP SOCKS5
# ---------------------------------------------------------------------------

_PYTHON_SOCKS_AVAILABLE = False
try:
    import socks  # PySocks — required for the WARP engine
    _PYTHON_SOCKS_AVAILABLE = True
except ImportError:
    pass

import requests


class DirectSession:
    """Plain HTTP GET with realistic browser headers.  No proxy.

    Best for: FNAIM, iad, ParuVendu, Notaires, 123webimmo.
    """

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(_BROWSER_HEADERS)

    def get(self, url: str, timeout: int = 20, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", timeout)
        return self._session.get(url, **kwargs)


class WarpSession:
    """HTTP GET routed through Cloudflare WARP SOCKS5 proxy.

    Why WARP?  The VPS runs on a cloud datacenter IP that many French
    portals blacklist.  WARP tunnels traffic out through Cloudflare's
    own network, so the destination sees a Cloudflare-affiliated IP that
    is far less likely to be pre-banned.  It is NOT a residential proxy
    and does not hide that the client is automated — that's why the
    stealth engine exists for the worst offenders.
    """

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(_BROWSER_HEADERS)
        self._proxies = {
            "http": _WARP_SOCKS5,
            "https": _WARP_SOCKS5,
        }
        self._session.proxies.update(self._proxies)

    @property
    def available(self) -> bool:
        """WARP only works if PySocks is importable."""
        return _PYTHON_SOCKS_AVAILABLE

    def get(self, url: str, timeout: int = 20, **kwargs) -> requests.Response:
        if not self.available:
            raise RuntimeError("PySocks not installed — run: pip install PySocks")
        kwargs.setdefault("timeout", timeout)
        return self._session.get(url, proxies=self._proxies, **kwargs)


class WebshareSession:
    """HTTP GET routed through a Webshare residential proxy.

    This is ENGINE 6 — the fast, JS-free Webshare variant.  It does NOT
    render JavaScript; it just fetches HTML over a residential egress IP.
    Use it for sites that block on IP reputation but don't need JS (i.e. the
    hard IP-blocks like Orpi's Cloudflare gate when the page HTML is server-
    rendered).

    Webshare proxies are HTTP proxies with per-IP credentials, format:
        http://USER:PASS@ip:port
    On the free plan we rotate over the fixed IP list in proxy_config.py.
    If you later upgrade to a *rotating residential* plan, you can instead
    point this at the single rotating gateway `p.webshare.io:80` and every
    request gets a fresh IP automatically.
    """

    def __init__(self, sticky: bool = False):
        self._session = requests.Session()
        self._session.headers.update(_BROWSER_HEADERS)
        self._proxies = None
        self._proxy_list = list(WEBSHARE_PROXY_LIST)
        self._sticky = sticky          # pin one IP for the whole session
        self._sticky_proxy = None      # the pinned IP (once chosen)
        self._sticky_proxy_ip = None   # the pinned IP string (chain mode)

    @property
    def available(self) -> bool:
        return bool(WEBSHARE_PROXY_LIST) and bool(WEBSHARE_PROXY_USER)

    def _next_proxy(self) -> dict:
        """Pick the next proxy (round-robin over the free IP list).

        In sticky mode the first choice is pinned and reused for every
        subsequent request — so a challenge request and the detail scrape of
        the same URL share one IP, which many WAFs expect (a single visitor
        from one IP, not rotating mid-session).
        """
        if self._sticky and self._sticky_proxy:
            return self._sticky_proxy
        if not self._proxy_list:
            self._proxy_list = list(WEBSHARE_PROXY_LIST)
        ip = self._proxy_list.pop(0)
        self._proxy_list.append(ip)  # rotate
        url = f"http://{WEBSHARE_PROXY_USER}:{WEBSHARE_PROXY_PASS}@{ip}"
        proxy = {"http": url, "https": url}
        if self._sticky and self._sticky_proxy is None:
            self._sticky_proxy = proxy
        return proxy

    def get(self, url: str, timeout: int = 20, **kwargs) -> requests.Response:
        if not self.available:
            raise RuntimeError("No Webshare proxies configured")
        # Chain through WARP: the VPS's direct route to Webshare IPs is
        # blocked (TCP timeout), but WARP reaches them.  See webshare_chain.py.
        try:
            from webshare_chain import ChainedWebshareSession
            ip = self._next_proxy_ip()
            cs = ChainedWebshareSession(proxy_ip=ip, timeout=timeout)
            cr = cs.get(url, timeout=timeout)
            # shape into a requests.Response-like object
            r = requests.Response()
            r.status_code = cr.status_code
            r._content = cr.text.encode("utf-8", errors="replace")
            r.headers = cr.headers
            r.url = url
            return r
        except ImportError:
            kwargs.setdefault("timeout", timeout)
            return self._session.get(url, proxies=self._next_proxy(), **kwargs)

    def _next_proxy_ip(self) -> str:
        """Pick the next proxy IP (round-robin, sticky-aware)."""
        if self._sticky and self._sticky_proxy_ip:
            return self._sticky_proxy_ip
        if not self._proxy_list:
            self._proxy_list = list(WEBSHARE_PROXY_LIST)
        ip = self._proxy_list.pop(0)
        self._proxy_list.append(ip)  # rotate
        if self._sticky and self._sticky_proxy_ip is None:
            self._sticky_proxy_ip = ip
        return ip


# ---------------------------------------------------------------------------
# ENGINE 3 — Lightpanda headless browser via fastCRW
# ---------------------------------------------------------------------------

def scrape_via_lightpanda(url: str, timeout_ms: int = 25000) -> dict:
    """Scrape a URL through fastCRW's Lightpanda browser.

    Lightpanda is a tiny JS-capable engine that can execute the scripts
    behind simple "checking your browser" gates, but it lacks a full DOM
    and GPU context — sophisticated WAFs fingerprint that.  When it works
    it's cheap and fast, so it sits between WARP and full Chromium.

    Returns a dict: success, markdown, rawHtml, status, error, blocked.
    """
    payload = {
        "url": url,
        "formats": ["markdown", "rawHtml"],
        "timeout": timeout_ms,
    }
    try:
        resp = requests.post(
            FASTCRW_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout_ms / 1000 + 15,
        )
        data = resp.json()
        if data.get("metadata", {}).get("statusCode") == 200:
            content = data.get("markdown", "")
            raw_html = data.get("rawHtml", "") or data.get("html", "")
            # A block page can still come back with HTTP 200 — check content
            content_blocked = any(p in content.lower() for p in _BLOCK_PHRASES)
            html_blocked = any(p in raw_html.lower() for p in _BLOCK_PHRASES) if raw_html else False
            blocked = content_blocked or html_blocked
            if blocked:
                return {
                    "success": False,
                    "markdown": content,
                    "rawHtml": raw_html,
                    "status": 200,
                    "error": "Cloudflare/WAF block page via Lightpanda — needs full browser",
                    "blocked": True,
                }
            return {
                "success": True,
                "markdown": content,
                "rawHtml": raw_html,
                "status": 200,
                "error": None,
                "renderDecision": data.get("renderDecision", {}),
            }
        error_msg = data.get("error") or data.get("metadata", {}).get("error")
        return {
            "success": False,
            "markdown": "",
            "rawHtml": "",
            "status": data.get("metadata", {}).get("statusCode"),
            "error": error_msg or resp.text[:500],
        }
    except requests.ConnectionError:
        return {"success": False, "markdown": "", "rawHtml": "", "status": 0,
                "error": "fastCRW unreachable", "blocked": False}
    except requests.Timeout:
        return {"success": False, "markdown": "", "rawHtml": "", "status": 0,
                "error": f"timeout after {timeout_ms}ms", "blocked": False}
    except Exception as e:
        return {"success": False, "markdown": "", "rawHtml": "", "status": 0,
                "error": str(e), "blocked": False}


# ---------------------------------------------------------------------------
# ENGINE 4 — Playwright + playwright-stealth (FULL Chromium)
# ---------------------------------------------------------------------------

# playwright-stealth patches the Chromium fingerprint so bot-detection
# heuristics (navigator.webdriver, plugins, languages, canvas, WebGL,
# CDP leaks) see a genuine human browser.  v2.x API:
#
#     from playwright_stealth import Stealth
#     stealth = Stealth()
#     stealth.apply_stealth_sync(page)   # page-level, sync API
#
# For async:  await stealth.apply_stealth_async(page)

_PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass

_PLAYWRIGHT_STEALTH_AVAILABLE = False
try:
    from playwright_stealth import Stealth
    _PLAYWRIGHT_STEALTH_AVAILABLE = True
except ImportError:
    pass

# Camoufox — patched Firefox anti-detect browser (a 2nd, distinct fingerprint)
_CAMOUFOX_AVAILABLE = False
try:
    import camoufox  # noqa: F401  (presence check)
    _CAMOUFOX_AVAILABLE = True
except ImportError:
    pass


def scrape_via_stealth(
    url: str,
    timeout_s: int = 45,
    use_warp: bool = True,
    wait_for_network: str = "domcontentloaded",
) -> dict:
    """Full-Chromium fetch with playwright-stealth, optionally via WARP.

    This is the engine that finally opens Orpi, SeLoger, Logic-Immo,
    Zilek and Superimmo, which all run Cloudflare's bot-management WAF.
    A real Chromium + stealth fingerprint passes their JS challenges.

    Design notes / pitfalls:
    - We launch Chromium with `headless=False`-equivalent args?  No — we
      keep headless=True but patch `navigator.webdriver` via stealth and
      add `--disable-blink-features=AutomationControlled`.  This defeats
      the vast majority of WAF fingerprint checks.
    - Cloudflare sometimes needs a couple of seconds to run its challenge
      script.  We wait for `networkidle` and then re-check the page title
      for the "Just a moment" marker; if still present we retry once with
      a fresh context.
    - If `use_warp` is True we chain the browser through the WARP SOCKS5
      proxy, giving Cloudflare both a clean IP *and* a clean fingerprint.
    - We never reuse a BrowserContext across sites — WAFs fingerprint
      cookies between requests.
    - The returned HTML is the fully rendered DOM (after JS execution),
      which is what we need for extraction.

    Returns a dict with success/rawHtml/status/error/blocked keys so the
    caller can treat it like the other engines.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return {"success": False, "rawHtml": "", "status": 0,
                "error": "playwright not installed", "blocked": False}
    if not _PLAYWRIGHT_STEALTH_AVAILABLE:
        return {"success": False, "rawHtml": "", "status": 0,
                "error": "playwright-stealth not installed", "blocked": False}

    try:
        from playwright_stealth import Stealth

        with sync_playwright() as p:
            # Launch Chromium headless with automation-controlled blink
            # feature disabled — this is the #1 WAF giveaway.
            launch_options = {
                "headless": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",  # some portals load cross-origin assets
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            }
            # Optional: chain through WARP SOCKS5 for a clean egress IP.
            # NOTE: Chromium only understands socks5:// (no remote-DNS
            # variant socks5h:// like curl/PySocks).  SOCKS5 with local DNS
            # is fine here because WARP is on the same docker network, so
            # DNS resolution of the target host happens inside the proxy
            # container anyway.
            if use_warp:
                launch_options["proxy"] = {
                    "server": f"socks5://{_WARP_HOST}:{_WARP_PORT}",
                    "username": "",
                    "password": "",
                }

            browser = p.chromium.launch(**launch_options)

            # Fresh context per attempt: no cookie carry-over between sites
            context = browser.new_context(
                user_agent=_BROWSER_HEADERS["User-Agent"],
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1366, "height": 768},
                extra_http_headers={
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                },
            )

            page = context.new_page()
            # Apply stealth patches to the page (navigator.webdriver=undefined,
            # real plugin list, canvas noise, etc.)
            Stealth().apply_stealth_sync(page)

            # Go!  We wait for DOMContentLoaded (not networkidle) because
            # many French portals keep long-polling requests open forever
            # (analytics, chat widgets, autocomplete) which would make
            # networkidle time out.  After load we sleep briefly so any
            # async listing content (and Cloudflare challenge) settles.
            page.goto(url, wait_until=wait_for_network, timeout=timeout_s * 1000)
            # Give the page's JS a beat to render dynamic content
            time.sleep(3.0)

            # If we landed on a "Just a moment" challenge page, wait for it
            # to clear (Cloudflare auto-submits after JS runs).
            for _ in range(3):
                title = page.title()
                if "just a moment" in title.lower() or "attention" in title.lower():
                    time.sleep(3.0)
                else:
                    break

            html = page.content()  # fully rendered DOM
            status = 200
            browser.close()

            # Check for block-page content sneaking through
            blocked = any(p in html.lower() for p in _BLOCK_PHRASES)
            if blocked and len(html) < 20000:
                return {"success": False, "rawHtml": html, "status": 200,
                        "error": "Cloudflare/WAF block page via stealth", "blocked": True}
            return {"success": True, "rawHtml": html, "status": status,
                    "error": None, "blocked": False}
    except PWTimeout as e:
        return {"success": False, "rawHtml": "", "status": 0,
                "error": f"playwright timeout: {e}", "blocked": False}
    except Exception as e:
        return {"success": False, "rawHtml": "", "status": 0,
                "error": f"playwright error: {type(e).__name__}: {e}", "blocked": False}


# ---------------------------------------------------------------------------
# ENGINE 5 — Playwright stealth THROUGH Webshare residential proxies
# ---------------------------------------------------------------------------

# The killer engine.  Most "hard" IP-blocks (Orpi's Cloudflare block,
# ASN/IP-level blocks) and many CAPTCHA layers only fail because the egress
# IP is a cloud datacenter IP.  Routing stealth Chromium through a Webshare
# residential IP gives both a clean fingerprint AND a residential egress IP —
# which cracks sites that datacenter-IP stealth cannot.
#
# Verified 2026-08-18: Orpi (previously: 403 on direct/warp/lightpanda/
# stealth) loads fully (544,500 chars, price 194 900 € extracted) via
# stealth Chromium + a Webshare residential proxy.

# Webshare residential proxy pool (single source of truth: config.py)
# Format: "ip:port".  These are HTTP proxies with per-IP auth.

def scrape_via_residential(url, timeout_s=40, max_proxies=4):
    """Fetch a URL through stealth Chromium + rotating Webshare residential IPs.

    Tries up to max_proxies residential IPs, returning the first successful
    (non-blocked) page.  A fresh BrowserContext is used per attempt to avoid
    cookie/fingerprint carry-over between IPs.

    Returns a dict with success/rawHtml/status/error/blocked keys, matching
    the other engines so scan_url() can treat it uniformly.
    """
    import random
    if not _PLAYWRIGHT_AVAILABLE or not _PLAYWRIGHT_STEALTH_AVAILABLE:
        return {"success": False, "rawHtml": "", "status": 0,
                "error": "playwright/stealth not installed", "blocked": False}

    # Shuffle so we don't always hit the same IP first
    proxies = random.sample(WEBSHARE_PROXY_LIST, min(max_proxies, len(WEBSHARE_PROXY_LIST)))
    last_err = ""

    from urllib.parse import urlparse as _uparse
    _dom = _uparse(url).netloc
    # Load cookie store helpers lazily (module may not exist in fresh clones)
    _cookies_mod = None
    try:
        from cookie_store import get_cookies, save_cookies
        _cookies_mod = (get_cookies, save_cookies)
    except ImportError:
        pass

    for proxy in proxies:
        ip, port = proxy.split(":")
        captured_cookies = []   # challenge cookies captured before context closes
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    executable_path=_CHROMIUM_PATH,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                    proxy={
                        "server": f"http://{ip}:{port}",
                        "username": WEBSHARE_PROXY_USER,
                        "password": WEBSHARE_PROXY_PASS,
                    },
                )
                try:
                    context = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris",
                                                  viewport={"width": 1366, "height": 900})
                    page = context.new_page()
                    Stealth().apply_stealth_sync(page)

                    # Reuse a saved challenge cookie for this (IP, domain) to
                    # skip re-solving the CAPTCHA on the next run.
                    if _cookies_mod:
                        saved = _cookies_mod[0](ip, _dom)
                        if saved:
                            try:
                                context.add_cookies(saved)
                                page.wait_for_timeout(500)
                            except Exception:
                                pass

                    # Behavioural warm-up: hit the site root first, then target,
                    # all on the SAME proxy IP (sticky — a single visitor from
                    # one IP, which is how real users arrive).
                    _human_warmup(page, _dom)
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
                    _human_scroll(page, steps=4)
                    page.wait_for_timeout(6000)
                    html = page.content()
                    status = 200
                    # Capture any anti-bot cookies (cf_clearance/datadome/etc.)
                    # BEFORE the context closes so we can persist them.
                    try:
                        captured_cookies = context.cookies()
                    except Exception:
                        captured_cookies = []
                finally:
                    browser.close()

            blocked = is_cloudflare_block(html, status)
            if not blocked:
                # Persist challenge cookies so the next run for this IP+domain
                # can skip the CAPTCHA entirely.
                if _cookies_mod and captured_cookies:
                    _cookies_mod[1](ip, _dom, captured_cookies)
                return {"success": True, "rawHtml": html, "status": status,
                        "error": None, "blocked": False, "proxy": proxy}
            last_err = f"block page via {proxy}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:80]} via {proxy}"
        time.sleep(2)  # gentle spacing between proxy attempts

    return {"success": False, "rawHtml": "", "status": 0,
            "error": f"all residential proxies failed: {last_err}", "blocked": True}


# ---------------------------------------------------------------------------
# ENGINE 6/7 SHARED — Behavioural layer (human-like interaction)
# ---------------------------------------------------------------------------
# Used by every browser engine.  Makes a headless browser look human:
#   - warm-up: load the site homepage first, then the target (real users
#     arrive via the home/search page, not a direct jump)
#   - Bézier mouse movements (not instant teleport clicks)
#   - natural scroll with jitter
#   - FR locale + realistic viewport already set by the caller
#
# These reduce the behavioural risk score that WAFs (especially DataDome and
# hCaptcha) compute.  Individually they rarely flip a block, but combined
# with a residential IP + clean fingerprint they push borderline cases over.

def _bezier_points(x0, y0, x1, y1, n=12):
    """Return n points on a curved path from (x0,y0) to (x1,y1).

    Uses a quadratic Bézier with a random control point offset, so the
    mouse path curves naturally instead of moving in a straight line
    (straight-line mouse movement is a strong automation signal).
    """
    import random
    # control point pulled perpendicular to the straight path
    cx = (x0 + x1) / 2 + random.uniform(-80, 80)
    cy = (y0 + y1) / 2 + random.uniform(-60, 60)
    pts = []
    for i in range(n + 1):
        t = i / n
        # quadratic bezier: B(t) = (1-t)^2 P0 + 2(1-t)t C + t^2 P1
        bx = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t ** 2 * x1
        by = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t ** 2 * y1
        pts.append((bx, by))
    return pts


def _human_mouse_move(page, x, y):
    """Move the mouse to (x,y) along a Bézier path with timing jitter."""
    import random
    import time as _t
    try:
        start = page.evaluate("() => ({x: window.innerWidth/2, y: window.innerHeight*0.7})")
        pts = _bezier_points(start["x"], start["y"], x, y)
        for px, py in pts:
            page.mouse.move(px, py)
            _t.sleep(random.uniform(0.008, 0.035))  # 8–35ms jitter per step
    except Exception:
        # if anything fails, just teleport (don't let behaviour break the fetch)
        page.mouse.move(x, y)


def _human_scroll(page, total=None, steps=5):
    """Scroll down the page in natural-sized steps with pauses."""
    import random
    import time as _t
    try:
        height = page.evaluate("document.body.scrollHeight") or 2000
        if total is None:
            total = min(height * 0.7, 1500)
        step = total / steps
        for _ in range(steps):
            page.mouse.wheel(0, step + random.uniform(-20, 20))
            _t.sleep(random.uniform(0.15, 0.5))
    except Exception:
        pass


def _human_warmup(page, domain):
    """Load the site root first so we arrive like a real user, not a bot."""
    import time as _t
    try:
        page.goto(f"https://{domain}/", wait_until="domcontentloaded", timeout=20000)
        _t.sleep(random.uniform(1.0, 2.5))
        _human_scroll(page, steps=3)
    except Exception:
        pass  # warm-up is best-effort; proceed to target regardless


# ---------------------------------------------------------------------------
# ENGINE 7 — Camoufox (Firefox anti-detect) + behavioural layer
# ---------------------------------------------------------------------------

def scrape_via_camoufox(url, timeout_s=45, use_proxy=False, warmup=True):
    """Fetch a URL through Camoufox — a patched Firefox with a unique
    fingerprint (navigator.webdriver=false natively, realistic canvas/UA).

    Camoufox gives us a SECOND browser engine with a totally different
    fingerprint from our Chromium stealth path.  Some WAFs (DataDome,
    hCaptcha risk-scoring) fingerprint Chromium harder than Firefox; having
    both lets us pick whichever the site is least aggressive towards.

    Behavioural layer is applied: warm-up on the site root, Bézier mouse
    movements, natural scroll.

    Args:
        url:       target URL
        timeout_s: max load time
        use_proxy: route through a Webshare residential IP (sticky per call)
        warmup:    do the homepage→target warm-up (recommended True)

    Returns dict with success/rawHtml/status/error/blocked keys.
    """
    import time
    import random
    if not _CAMOUFOX_AVAILABLE:
        return {"success": False, "rawHtml": "", "status": 0,
                "error": "camoufox not installed", "blocked": False}
    from urllib.parse import urlparse
    domain = urlparse(url).netloc

    # Optional residential proxy (sticky — one IP for this whole session)
    proxy_conf = None
    if use_proxy and WEBSHARE_PROXY_LIST:
        ip = random.choice(WEBSHARE_PROXY_LIST)
        # Credentials MUST be passed separately (Playwright rejects creds
        # embedded in the server URL — ERR_INVALID_AUTH_CREDENTIALS).
        proxy_conf = {"server": f"http://{ip}", "username": WEBSHARE_PROXY_USER,
                      "password": WEBSHARE_PROXY_PASS}

    try:
        from camoufox.sync_api import Camoufox

        launch = {"headless": True, "locale": "fr-FR", "humanize": True,
                  "geoip": True}  # geoip=True ties locale/timezone to the proxy IP
        if proxy_conf:
            launch["proxy"] = proxy_conf

        with Camoufox(**launch) as browser:
            page = browser.new_page()

            if warmup:
                _human_warmup(page, domain)

            page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            # let any challenge / async content settle
            for _ in range(4):
                title = page.title()
                if "un instant" in title.lower() or "just a moment" in title.lower() \
                   or "humain" in title.lower():
                    time.sleep(2.5)
                else:
                    break
            # gentle human scroll to trigger any lazy content
            _human_scroll(page, steps=4)
            time.sleep(1.0)

            html = page.content()
            status = 200
            blocked = is_cloudflare_block(html, status)
            return {"success": not blocked, "rawHtml": html, "status": status,
                    "error": None if not blocked else "block page via camoufox",
                    "blocked": blocked, "proxy": proxy_conf}
    except Exception as e:
        return {"success": False, "rawHtml": "", "status": 0,
                "error": f"camoufox error: {type(e).__name__}: {str(e)[:120]}",
                "blocked": False}


# ---------------------------------------------------------------------------
# ENGINE 9 — DataDome bypass via 2Captcha (SeLoger / Logic-Immo)
# ---------------------------------------------------------------------------

def _parse_datadome_dd(body: str) -> dict:
    """Extract the DataDome `var dd={...}` object from a challenge page.

    The dd object carries the challenge fields (cid, hsh, t, s, e, host)
    needed to build the geo.captcha-delivery.com solve URL.

    Parsing is layered (robust to the single-quote / nested-string quirks
    DataDome serves):
      1. ast.literal_eval — handles single-quoted JSON with nested quotes safely
      2. json.loads after swapping single→double quotes (only if no inner
         apostrophes that would break it)
      3. key:value regex fallback for the fields we actually need
    """
    import ast
    import json
    import re

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
                # 1) ast.literal_eval — Python-style single-quote literals
                try:
                    val = ast.literal_eval(raw)
                    if isinstance(val, dict):
                        return val
                except Exception:
                    pass
                # 2) json after single→double quote swap (safe if no apostrophes)
                if "'" in raw and '"' not in raw:
                    try:
                        return json.loads(raw.replace("'", '"'))
                    except Exception:
                        pass
                # 3) regex fallback — pull exactly the fields we need
                fields = dict(re.findall(r"[\"'](\w+)[\"']\s*:\s*[\"']([^\"']*)[\"']", raw))
                # keep only non-empty, known keys
                return {k: v for k, v in fields.items()
                        if k in ("cid", "hsh", "t", "s", "e", "host", "rt", "qp") and v}
    return {}


def _build_datadome_challenge_url(dd: dict, page_url: str) -> str:
    """Build the geo.captcha-delivery.com challenge URL from dd fields."""
    import urllib.parse
    cid = dd.get("cid", "")
    hsh = dd.get("hsh", "")
    t = dd.get("t", "fe")
    s_val = dd.get("s", "")
    e_val = dd.get("e", "")
    host = dd.get("host", "geo.captcha-delivery.com")
    referer = urllib.parse.quote(page_url, safe="")
    return (f"https://{host}/captcha/?initialCid={urllib.parse.quote(cid)}"
            f"&hash={hsh}&cid={urllib.parse.quote(cid)}&t={t}"
            f"&referer={referer}&s={s_val}&e={e_val}")


def scrape_via_datadome(url, timeout_s=60, user_agent=None, proxy=None):
    """Bypass DataDome (SeLoger / Logic-Immo) by solving it via 2Captcha.

    DataDome is NOT an image captcha — it's a behavioural + token system.
    The flow (verified live 2026-08-20, ~$0.0015/solve):
      1. fetch the URL through the WARP→Webshare chain → DataDome serves a
         403 page containing `var dd={...}` with challenge fields
      2. build the geo.captcha-delivery.com challenge URL from those fields
      3. submit to 2Captcha with the SAME proxy + UA so the returned
         datadome cookie matches our egress IP
      4. re-fetch the URL carrying the datadome cookie → real listing HTML

    This is a PAID service.  Used ONLY when the early-routing classifier
    sends 'datadome' here.  Conservative — one solve per URL.

    Args:
        url:       target SeLoger/Logic-Immo URL
        timeout_s: per-request timeout
        user_agent: UA to use (must match across fetch+solve)
        proxy:     "ip:port" Webshare proxy (optional; if None we still try
                   with the direct egress — DataDome may reject, but worth it
                   when no proxy is available)

    Returns ScrapeResult with success/rawHtml/status/error/blocked/reused_cookie.
    """
    import re
    from twocaptcha_client import solve_datadome
    from webshare_chain import RotatingWebshareSession

    if user_agent is None:
        # One consistent fingerprint for solve + all requests (review item 16)
        try:
            from config import build_fingerprint
            user_agent = build_fingerprint()["user_agent"]
        except Exception:
            user_agent = _BROWSER_HEADERS["User-Agent"]

    # Use the rotating residential plan with a French + sticky session.
    # This is the proven winner: the same French residential IP for BOTH the
    # 2Captcha solve and the refetch, so the IP-bound datadome cookie matches.
    # Some French IPs are flagged by DataDome (solve returns UNSOLVABLE) — we
    # cycle through sessions 1..8 until one solves.
    try:
        from config import WEBSHARE_ROTATE_SESSIONS
        max_sessions = max(1, int(WEBSHARE_ROTATE_SESSIONS))
    except Exception:
        max_sessions = 8
    sessions_to_try = list(range(1, min(max_sessions, 9) + 1))
    if proxy is not None and isinstance(proxy, int):
        # caller passed a session number directly
        sessions_to_try = [proxy]

    last_error = None
    for sess in sessions_to_try:
        try:
            result = _datadome_via_rotate(
                url=url, timeout_s=timeout_s, user_agent=user_agent,
                session=sess, country="fr")
            if result.success:
                return result
            last_error = result.error or f"session fr-{sess} failed"
            log_info(f"datadome session fr-{sess} failed: {last_error}")
        except Exception as e:
            last_error = f"session fr-{sess}: {type(e).__name__}: {str(e)[:80]}"
            log_info(last_error)
    return ScrapeResult(success=False, status=0, engine="datadome",
                        blocked=True, error=f"all rotating sessions failed: {last_error}")


def _datadome_via_rotate(url: str, timeout_s: int, user_agent: str,
                         session: int, country: str = "fr") -> ScrapeResult:
    """Solve + fetch DataDome through one rotating sticky session.

    Uses RotatingWebshareSession(country=country, session=session) for BOTH
    the challenge fetch, the 2Captcha solve (SOCKS5 proxytype — proven to
    solve, HTTP returns UNSOLVABLE), and the refetch carrying the cookie.
    """
    import re
    from twocaptcha_client import solve_datadome
    from webshare_chain import RotatingWebshareSession
    from cookie_store import get_cookies, save_cookies

    domain = urllib.parse.urlparse(url).netloc

    def _new_session(cookie: str | None = None) -> RotatingWebshareSession:
        hdrs = {"User-Agent": user_agent, "Accept-Language": "fr-FR,fr;q=0.9"}
        if cookie:
            hdrs["Cookie"] = f"datadome={cookie}"
        return RotatingWebshareSession(session=session, timeout=timeout_s,
                                       country=country, headers=hdrs)

    # Step 0: try a saved cookie first (IP-bound; cookie_store keyed by the
    # sticky session username + domain).
    proxy_key = f"{country}-{session}"
    saved_dd = None
    for c in get_cookies(proxy_key, domain):
        if c.get("name") == "datadome":
            saved_dd = c.get("value")
            break
    if saved_dd:
        s0 = _new_session(saved_dd)
        r0 = s0.get(url)
        if not is_cloudflare_block(r0.text, r0.status_code) and len(r0.text) > 3000:
            return ScrapeResult(success=True, html=r0.text, status=r0.status_code,
                                engine="datadome", blocked=False,
                                proxy_ip=f"{country}-{session}", reused_cookie=True)

    # Step 1: fetch to get the dd challenge object
    s = _new_session()
    r = s.get(url)
    dd = _parse_datadome_dd(r.text)
    if not dd:
        if not is_cloudflare_block(r.text, r.status_code) and len(r.text) > 3000:
            return ScrapeResult(success=True, html=r.text, status=r.status_code,
                                engine="datadome", blocked=False,
                                proxy_ip=f"{country}-{session}")
        return ScrapeResult(success=False, html=r.text, status=r.status_code,
                            engine="datadome", blocked=True,
                            error="no DataDome dd object found",
                            proxy_ip=f"{country}-{session}")
    captcha_url = _build_datadome_challenge_url(dd, url)
    log_debug(f"datadome fr-{session} dd={ {k: dd.get(k) for k in ('cid','hsh','t','s','e','host','rt')} }")
    log_info(f"datadome fr-{session} captcha_url={captcha_url[:110]}...")

    # Step 2: solve via 2Captcha using SOCKS5 proxytype through the SAME session
    cookie_set = solve_datadome(captcha_url=captcha_url, page_url=url,
                                user_agent=user_agent,
                                proxy=s.proxy_auth_str(), proxytype="socks5")
    if not cookie_set or cookie_set.startswith("ERROR"):
        return ScrapeResult(success=False, status=0, engine="datadome",
                            blocked=True, proxy_ip=f"{country}-{session}",
                            error=f"2captcha solve failed: {cookie_set}")
    mm = re.search(r"datadome=([^;]+)", cookie_set)
    if not mm:
        return ScrapeResult(success=False, status=0, engine="datadome",
                            blocked=True, proxy_ip=f"{country}-{session}",
                            error="no datadome cookie in solve response")

    # Step 3: refetch carrying the cookie through the SAME session
    s2 = _new_session(mm.group(1))
    r2 = s2.get(url)
    blocked = is_cloudflare_block(r2.text, r2.status_code)
    has_data = len(r2.text) > 3000 and ("annonce" in r2.text.lower()
                                        or "prix" in r2.text.lower())
    success = (not blocked and has_data)
    if success:
        save_cookies(proxy_key, domain,
                     [{"name": "datadome", "value": mm.group(1),
                       "domain": "." + domain, "path": "/"}])
    return ScrapeResult(success=success, html=r2.text, status=r2.status_code,
                        engine="datadome", blocked=blocked,
                        proxy_ip=f"{country}-{session}", reused_cookie=False,
                        error=None if success
                        else f"still blocked after solve (status {r2.status_code})")


# ---------------------------------------------------------------------------
# BLOCK DETECTION — shared by all engines
# ---------------------------------------------------------------------------

def is_cloudflare_block(html: str, status: int) -> bool:
    """Heuristic detection of Cloudflare/WAF block pages.

    Returns True if the response is a block page rather than real content.
    Handles both HTTP status codes (403/451/503+Cloudflare) and the
    content markers Cloudflare injects into challenge pages.

    Also flags the DataDome CAPTCHA shells that SeLoger/Logic-Immo serve:
    a real property-listing page is always >2KB of content, whereas a
    DataDome/Cloudflare challenge shell is a ~1.5KB stub.  So a suspiciously
    small 200 page with no extracted content is treated as a block.
    """
    if status == 403 or status == 451:
        return True
    low = html.lower()
    for phrase in _BLOCK_PHRASES:
        if phrase in low:
            return True
    if '<title>just a moment' in low:
        return True
    if status == 503 and 'cloudflare' in low:
        return True
    # DataDome / CAPTCHA shell detection — tiny stub page, no real listing
    if status == 200 and len(html) < 3000:
        # A genuine listing page is much larger.  Treat tiny 200s as blocks.
        if 'captcha' in low or 'datadome' in low or 'geo.captcha-delivery.com' in low \
           or 'un instant' in low or 'prouvez que' in low \
           or 'request could not be satisfied' in low or 'cloudfront' in low and 'error' in low:
            return True
    return False


def diagnosis(html: str, status: int, engine: str) -> str:
    """One-line human-readable verdict for the diagnostics log."""
    if status == 200 and not is_cloudflare_block(html, status):
        return f"OK — {engine}, {len(html)} chars, 200 OK"
    if is_cloudflare_block(html, status):
        return f"BLOCKED — Cloudflare/WAF block via {engine} (status {status}, {len(html)} chars)"
    if status == 0:
        return f"FAILED — connection error via {engine}"
    return f"UNEXPECTED — status {status} via {engine}"


# ---------------------------------------------------------------------------
# CHALLENGE CLASSIFIER — detect which anti-bot we hit, route to the best engine
# ---------------------------------------------------------------------------

def classify_challenge(html: str, status: int) -> str:
    """Return a short label for the anti-bot protection present in a response.

    Returns one of:
      'datadome'   — DataDome CAPTCHA (SeLoger, Logic-Immo)
      'hcaptcha'   — hCaptcha checkbox/challenge (Superimmo)
      'cloudflare' — Cloudflare challenge ("Just a moment" / "Un instant…")
      'turnstile'  — Cloudflare Turnstile (Zilek)
      'hard403'    — bare 403 (IP-reputation block, no challenge page)
      'ok'         — real content, not a block
      'unknown'    — couldn't classify

    The classifier inspects headers/body markers.  The scan_url() cascade uses
    this to JUMP straight to the strongest engine for the detected challenge
    type instead of burning the full cascade (early routing).
    """
    if status in (403, 451):
        return "hard403"
    low = html.lower()

    # DataDome — distinctive iframe + host
    if "datadome" in low or "geo.captcha-delivery.com" in low \
       or "x-datadome" in low or "datadome" in html:
        return "datadome"
    # hCaptcha — the widget + sitekey
    if "hcaptcha.com" in low or "h-captcha" in low or "data-sitekey" in low:
        return "hcaptcha"
    # Cloudflare Turnstile (Zilek) — turnstile token / cf-challenge
    if "turnstile" in low or "cf-challenge" in low:
        return "turnstile"
    # Generic Cloudflare interstitial
    if "just a moment" in low or "un instant" in low or "checking your browser" in low \
       or "verify you are human" in low or "attention required" in low \
       or "cf-chl" in low or "__cf_chl" in low:
        return "cloudflare"
    if "prouvez que" in low or "êtes un humain" in low or "vous n'êtes pas un robot" in low:
        return "hcaptcha"
    if status == 200 and len(html) > 3000 and not is_cloudflare_block(html, status):
        return "ok"
    return "unknown"


# Best engine to try first for each challenge type (early routing).
# Fast/cheap first, escalate to residential/stealth only as needed.
_CHALLENGE_ROUTING = {
    "datadome":   ["datadome", "camoufox", "webshare-stealth", "stealth"],  # 2Captcha solve first
    "hcaptcha":   ["camoufox", "webshare-stealth", "stealth"],
    "turnstile":  ["camoufox", "webshare-stealth", "stealth"],
    "cloudflare": ["stealth", "webshare", "webshare-stealth", "camoufox"],
    "hard403":    ["webshare", "webshare-stealth", "camoufox"],  # IP block → residential
    "ok":         [],
    "unknown":    ["webshare", "stealth", "camoufox"],
}


def route_engines(challenge_type: str, full_cascade: list[str],
                  domain: str | None = None) -> list[str]:
    """Return an optimised engine order for the detected challenge type.

    Adaptive: if we have historical success metrics for this (domain,
    challenge) pair, order engines by success rate (best first).  Otherwise
    use the static routing table (most-likely-to-succeed engines first) and
    finally fall back to the provided full cascade.

    Domain hints (review item 9): SeLoger / Logic-Immo are JS SPAs — the free
    Camoufox render cracks them (verified 2026-08-20, 1.15MB real listings)
    without spending on a 2Captcha DataDome solve.  Prefer it.
    """
    _SPA_DOMAINS = {"www.seloger.com", "www.logic-immo.com"}
    if domain in _SPA_DOMAINS and challenge_type in ("hard403", "datadome"):
        ordered = ["camoufox", "datadome", "webshare-stealth", "stealth"]
        try:
            from engine_metrics import best_engine_for
            hist = best_engine_for(domain, challenge_type)
            if hist and hist != _DEFAULT_ORDER.get(challenge_type, []):
                # historical data beats the hint once it exists
                return hist
        except Exception:
            pass
        return ordered
    if challenge_type in _CHALLENGE_ROUTING and _CHALLENGE_ROUTING[challenge_type]:
        if domain:
            try:
                from engine_metrics import best_engine_for
                return best_engine_for(domain, challenge_type)
            except Exception:
                pass
        return _CHALLENGE_ROUTING[challenge_type]
    return full_cascade


# ---------------------------------------------------------------------------
# HTML → STRUCTURED DATA EXTRACTION
# ---------------------------------------------------------------------------

# French property portals are inconsistent about price markup.  We use a
# stack of regex strategies, from most-specific to most-generic, so each
# portal has a chance to be parsed correctly.

# Generic: "prix : 123 456 €" or microdata <span itemprop="price">123 456</span>
_PRICE_RE = re.compile(
    r"(?:"
    r"(?:prix|price|à vendre|soldée?|prix du bien est de)\s*[.:]?\s*"
    r"|<span[^>]*itemprop=\"price\"[^>]*>\s*"
    r")?"
    r"(\d[\d.,?\s]{1,10})\s*(?:€|&euro;|Euro|</span>)",
    re.IGNORECASE
)

# FNAIM-specific microdata: <span itemprop="price">79 900</span>
_FNAIM_PRICE_RE = re.compile(
    r"<span[^>]*itemprop=\"price\"[^>]*>\s*(\d[\d\s.,]*)\s*</span>",
    re.IGNORECASE
)

# FNAIM H1/title: "Achat Immeuble 103m² LAON 02000 79 900 €"
_FNAIM_TITLE_PRICE_RE = re.compile(
    r"(\d{2,3}(?:\s+\d{3})?)\s*(?:&amp;)?\s*euro",
    re.IGNORECASE
)

# Surface: "123 m²" (square-metre symbol, possibly HTML-encoded)
_SURFACE_RE = re.compile(r"(\d+)\s*m²|surface\s*(?:habitable|de\s*)?[:.]?\s*(\d+)", re.IGNORECASE)

# DPE energy label: "DPE : F", "classe énergétique E", "ÉTIQUETTE ÉNERGIE-CLIMAT E"
_DPE_RE = re.compile(
    r"(?:DPE\s*[.:]?\s*|classe\s*(?:énergétique|energetique)\s*[.:]?\s*|"
    r"ÉTIQUETTE\s*ÉNERGIE[^<]{0,30})"
    r"([A-G])\s",
    re.IGNORECASE
)

# Agency / agent name
_AGENCY_RE = re.compile(r"(?:agence|conseiller|agent)\s*(?:de|immobilier|:\s*)([^<\n,]{2,60})", re.IGNORECASE)

# Ad reference number
_REF_RE = re.compile(r"(?:réf|référence|ref)[.:]?\s*([A-Z0-9\-]{6,40})", re.IGNORECASE)


def _extract_structured_data(html: str) -> dict:
    """Structured-data extraction (review item 7, layer 1 of 3).

    Tries, in order:
      1. <script type="application/ld+json"> — schema.org Product/Offer/RealEstateListing
      2. microdata <meta itemprop=...> / <span itemprop=...>

    Returns a dict with price_eur / surface_m2 / dpe_energy if found,
    else an empty dict.  This is the highest-priority extraction source
    because it's schema-typed and survives portal markup changes far
    longer than regex (which has a ~2-6 week half-life on these sites).
    """
    import json as _json
    result = {}

    # --- Layer 1a: application/ld+json ---
    for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         html, re.S | re.I):
        raw = m.group(1).strip()
        try:
            data = _json.loads(raw)
        except Exception:
            # sometimes wrapped in /*<![CDATA[*/ ... */
            try:
                data = _json.loads(raw.replace("/*<![CDATA[*/", "").replace("*/]]>", "").strip())
            except Exception:
                continue
        # walk nested objects/arrays for price/surface fields
        def _walk(node):
            nonlocal result
            if isinstance(node, dict):
                # price in EUR
                if "offers" in node and isinstance(node["offers"], dict):
                    off = node["offers"]
                    p = off.get("price") or off.get("priceSpecification", {}).get("price")
                    if p is not None and not result.get("price_eur"):
                        try:
                            result["price_eur"] = int(float(str(p).replace(",", ".")))
                        except (ValueError, TypeError):
                            pass
                # surface
                floor = node.get("floorSize") or node.get("numberOfRooms")
                if isinstance(floor, dict):
                    v = floor.get("value")
                    if v and not result.get("surface_m2"):
                        try:
                            result["surface_m2"] = int(float(str(v)))
                        except (ValueError, TypeError):
                            pass
                # DPE energy
                for k in ("energyRating", "energyEfficiency", "efficientEnergy",
                          "energyConsumption", "hasEnergyEfficiency"):
                    v = node.get(k)
                    if isinstance(v, dict):
                        v = v.get("value") or v.get("name")
                    if isinstance(v, str) and v and not result.get("dpe_energy"):
                        m2 = re.search(r"\b([A-G])\b", v)
                        if m2:
                            result["dpe_energy"] = m2.group(1).upper()
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)
        _walk(data)

    # --- Layer 1b: microdata meta itemprop ---
    if not result.get("price_eur"):
        pm = re.search(
            r'<meta[^>]*itemprop=["\']price["\'][^>]*content=["\']([\d\s.,]+)["\']',
            html, re.I)
        if pm:
            try:
                result["price_eur"] = int(float(pm.group(1).replace(" ", "").replace(",", ".")))
            except (ValueError, TypeError):
                pass
    if not result.get("surface_m2"):
        sm = re.search(
            r'<meta[^>]*itemprop=["\']floorSize|"surface"["\'][^>]*content=["\'](\d+)["\']',
            html, re.I)
        if sm:
            try:
                result["surface_m2"] = int(sm.group(1))
            except (ValueError, TypeError):
                pass

    return result


def extract_property_data(html: str, url: str) -> dict:
    """Best-effort extraction of key fields from French property listing HTML.

    Returns a dict with price, surface, DPE, agency, ref, description.
    Extraction is layered (review item 7):
      1. structured data (LD+JSON / microdata) — most reliable, survives long
      2. site CSS selectors (Orpi, SeLoger, FNAIM, iad, ParuVendu)
      3. regex fallback (broadest but most fragile)
    Not all fields will be populated — structure varies wildly per portal.
    """
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    # --- Layer 1: structured data (LD+JSON / microdata) ---
    struct = _extract_structured_data(html)

    # --- Layer 2: site-specific CSS selectors via lxml ---
    from lxml import html as _lhtml
    price = struct.get("price_eur")
    surface = struct.get("surface_m2")
    dpe = struct.get("dpe_energy")
    try:
        tree = _lhtml.fromstring(html)
        if price is None:
            for sel in ('[itemprop="price"]', '[data-testid="price"]',
                        '[class*="price"]', '.prix', '[class*="amount"]'):
                els = tree.cssselect(sel)
                if els:
                    t = " ".join(els[0].itertext()).strip()
                    mm = re.search(r"([\d][\d\s.,]*)\s*(?:€|&euro;|euro)", t, re.I)
                    if mm:
                        try:
                            price = int(float(mm.group(1).replace(" ", "").replace(",", ".")))
                        except (ValueError, TypeError):
                            pass
                    if price:
                        break
        if surface is None:
            for sel in ('[itemprop="floorSize"]', '[data-testid="surface"]',
                        '[class*="surface"]', '[class*="area"]'):
                els = tree.cssselect(sel)
                if els:
                    t = " ".join(els[0].itertext()).strip()
                    mm = re.search(r"(\d+)\s*m²?", t, re.I)
                    if mm:
                        try:
                            surface = int(mm.group(1))
                        except (ValueError, TypeError):
                            pass
                    if surface:
                        break
    except Exception:
        pass  # malformed HTML — fall through to regex

    # --- Layer 3: regex fallback (existing strategies) ---
    # Price — try multiple strategies in priority order
    if price is None:
        # FNAIM microdata <span itemprop="price">XX XXX</span>
        fnaim_match = _FNAIM_PRICE_RE.search(html)
        if fnaim_match:
            p = fnaim_match.group(1).replace(" ", "").replace(".", "")
            try:
                price = int(p)
            except ValueError:
                pass
    if price is None:
        price_match = _PRICE_RE.search(text)
        if price_match:
            p = price_match.group(1).replace(" ", "").replace(".", "")
            try:
                price = int(p)
            except ValueError:
                pass
    if price is None:
        title_match = _FNAIM_TITLE_PRICE_RE.search(text)
        if title_match:
            p = title_match.group(1).replace(" ", "")
            try:
                price = int(p)
            except ValueError:
                pass

    # Surface (regex fallback)
    if surface is None:
        surf_match = _SURFACE_RE.search(text)
        if surf_match:
            groups = surf_match.groups()
            if groups[0]:
                try:
                    surface = int(groups[0])
                except ValueError:
                    pass
            elif groups[1]:
                try:
                    surface = int(groups[1])
                except ValueError:
                    pass

    # DPE (regex fallback)
    if dpe is None:
        dpe_match = _DPE_RE.search(text)
        if dpe_match:
            dpe = dpe_match.group(1).upper()

    # Agency / agent
    agency_raw = _AGENCY_RE.search(text)

    # Reference number
    ref_raw = _REF_RE.search(text)

    # Price-per-m² with outlier flag (review item 7)
    price_per_m2 = (price / surface) if (price and surface and surface > 0) else None
    outlier = None
    if price_per_m2 is not None:
        if price_per_m2 > 8000:
            outlier = "high"
        elif price_per_m2 < 300:
            outlier = "low"

    return {
        "url": url,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "price_eur": price,
        "surface_m2": surface,
        "price_per_m2": price_per_m2,
        "price_outlier": outlier,
        "dpe_energy": dpe,
        "agency": agency_raw.group(1).strip() if agency_raw else None,
        "reference": ref_raw.group(1) if ref_raw else None,
        "description_snippet": text[:800].strip(),
        "full_html_length": len(html),
    }


# ---------------------------------------------------------------------------
# PER-SITE KNOWN DETAILS (priors from research)
# ---------------------------------------------------------------------------

# These were extracted from earlier research — used as a cross-check when
# new scans succeed, to catch silent extraction bugs (e.g. price captured
# from the wrong part of the page).

KNOWN_LISTINGS = {
    "orpi_laon_230m2": {
        "url": "https://www.orpi.com/annonce-vente-immeuble-t5-laon-02000-8ab73979-6aa3-41bd-bb2f-89630451e57b/",
        "expected_price": 194900,
        "expected_surface": 180,
        "expected_type": "immeuble commerce + habitation",
        "expected_dpe": None,
    },
    "iad_laon_230m2": {
        "url": "https://www.iadfrance.fr/annonce/immeuble-vente-laon-230m2/r2041287",
        "expected_price": 219000,
        "expected_surface": 230,
        "expected_type": "local commercial + 4 apparts + T3",
        "expected_dpe": "D",
    },
}


# ---------------------------------------------------------------------------
# SCANNER — single URL, multi-engine cascade
# ---------------------------------------------------------------------------

def scan_url(
    url: str,
    engines: list[str] | None = None,
    timeout_s: int = 25,
    log_lines: list[str] | None = None,
    stealth_use_warp: bool = True,
    camoufox_proxy: bool = False,
    explicit: bool = False,
) -> dict:
    """Scan a single property URL using the requested engines.

    engines: ['direct', 'warp', 'lightpanda', 'stealth'] — defaults to
    the full cascade in that order.  The first engine returning a
    non-blocked page wins; its raw HTML + extracted data are returned.

    Returns a dict with url, engine, status, blocked, html_length, data,
    raw_html, diagnostics, scan_time_s.
    """
    if log_lines is None:
        log_lines = []
    if engines is None:
        engines = ["direct", "warp", "lightpanda", "stealth", "webshare", "webshare-stealth"]

    log_lines.append(f"[{datetime.now(timezone.utc).isoformat()}Z] Scanning: {url}")
    log_lines.append(f"  Engines: {', '.join(engines)}")
    log_lines.append("")

    # ---- EARLY ROUTING (challenge classifier) ------------------------------
    # Do ONE cheap detection pass (direct HTTP) to learn which anti-bot the
    # site uses, then reorder the cascade so the strongest engine for that
    # challenge type runs FIRST instead of burning every cheap engine first.
    # If the cheap probe already returns real content, we're done immediately.
    probe_html, probe_status = "", 0
    try:
        _probe = DirectSession()
        _pr = _probe.get(url, timeout=min(timeout_s, 15))
        probe_html, probe_status = _pr.text, _pr.status_code
    except Exception:
        pass

    # Only apply challenge-based early routing when NOT an explicit single-
    # engine request (user asked for a specific engine → honour it exactly).
    if not explicit:
        challenge = classify_challenge(probe_html, probe_status)
        if challenge == "ok" and len(probe_html) > 3000:
            log_lines.append(f"  → direct probe OK ({len(probe_html)} chars) — no challenge")
            crash_data = {"success": True, "markdown": "", "rawHtml": probe_html,
                          "status": probe_status, "error": None}
            extracted = extract_property_data(probe_html, url)
            return {
                "url": url, "engine": "direct", "status": probe_status,
                "blocked": False, "html_length": len(probe_html),
                "data": extracted, "raw_html": probe_html,
                "diagnostics": log_lines, "scan_time_s": 0.0,
            }
        elif challenge != "unknown":
            _domain = urllib.parse.urlparse(url).netloc
            ordered = route_engines(challenge, engines, domain=_domain)
            log_lines.append(f"  → challenge classifier: {challenge} — routing to: "
                             f"{', '.join(ordered)}")
            engines = ordered

    crash_data = {}
    crash_reason = ""

    for engine in engines:
        log_lines.append(f"  [Engine: {engine}]")
        start = time.time()

        try:
            if engine == "direct":
                sess = DirectSession()
                resp = sess.get(url, timeout=timeout_s)
                html = resp.text
                status = resp.status_code
                log_lines.append(f"    Status: {status}, size: {len(html)} chars")
                crash_data = {"success": True, "markdown": "", "rawHtml": html,
                              "status": status, "error": None}
                crash_reason = ""

            elif engine == "warp":
                if not WarpSession().available:
                    log_lines.append(f"    SKIP — PySocks not installed")
                    continue
                sess = WarpSession()
                resp = sess.get(url, timeout=timeout_s)
                html = resp.text
                status = resp.status_code
                log_lines.append(f"    Status: {status}, size: {len(html)} chars")
                crash_data = {"success": True, "markdown": "", "rawHtml": html,
                              "status": status, "error": None}
                crash_reason = ""

            elif engine == "lightpanda":
                log_lines.append(f"    Calling fastCRW Lightpanda (timeout {timeout_s}s)...")
                crash_data = scrape_via_lightpanda(url, timeout_ms=timeout_s * 1000)
                html = crash_data.get("rawHtml", "")
                status = crash_data.get("status", 0)

            elif engine == "stealth":
                log_lines.append(f"    Calling Playwright stealth Chromium (timeout {timeout_s}s, "
                                 f"warp={'yes' if stealth_use_warp else 'no'})...")
                crash_data = scrape_via_stealth(url, timeout_s=timeout_s, use_warp=stealth_use_warp)
                html = crash_data.get("rawHtml", "")
                status = crash_data.get("status", 0)

            elif engine == "residential":
                log_lines.append(f"    Calling stealth Chromium + Webshare residential IPs...")
                crash_data = scrape_via_residential(url, timeout_s=timeout_s)
                html = crash_data.get("rawHtml", "")
                status = crash_data.get("status", 0)

            elif engine == "webshare":           # ENGINE 6 — fast HTTP via Webshare
                if not WebshareSession().available:
                    log_lines.append(f"    SKIP — no Webshare proxies configured")
                    continue
                sess = WebshareSession(sticky=True)   # pin one IP for this URL's session
                resp = sess.get(url, timeout=timeout_s)
                html = resp.text
                status = resp.status_code
                log_lines.append(f"    Status: {status}, size: {len(html)} chars (via Webshare HTTP)")
                crash_data = {"success": True, "markdown": "", "rawHtml": html,
                              "status": status, "error": None}
                crash_reason = ""

            elif engine == "webshare-stealth":   # ENGINE 7 — stealth Chromium via Webshare
                log_lines.append(f"    Calling stealth Chromium + Webshare residential IPs...")
                crash_data = scrape_via_residential(url, timeout_s=timeout_s)
                html = crash_data.get("rawHtml", "")
                status = crash_data.get("status", 0)

            elif engine == "camoufox":            # ENGINE 8 — Firefox anti-detect + behaviour
                log_lines.append(f"    Calling Camoufox (Firefox anti-detect, warm-up + behaviour, "
                                 f"proxy={'yes' if camoufox_proxy else 'no'})...")
                crash_data = scrape_via_camoufox(url, timeout_s=timeout_s,
                                                 use_proxy=camoufox_proxy, warmup=True)
                html = crash_data.get("rawHtml", "")
                status = crash_data.get("status", 0)

            elif engine == "datadome":            # ENGINE 9 — 2Captcha DataDome solve
                log_lines.append(f"    Calling 2Captcha DataDome solve (PAID ~$0.003)...")
                crash_data = scrape_via_datadome(url, timeout_s=timeout_s)
                html = crash_data.get("rawHtml", "")
                status = crash_data.get("status", 0)

            # Analyse
            blocked = is_cloudflare_block(html, status)
            diag = diagnosis(html, status, engine)

            # Record engine outcome for adaptive routing (review item 6/13)
            try:
                from engine_metrics import record_outcome
                _chal = classify_challenge(html, status)
                record_outcome(
                    domain=urllib.parse.urlparse(url).netloc,
                    challenge=_chal, engine=engine,
                    success=(not blocked and len(html) > 500),
                    timing_s=time.time() - start,
                    proxy_ip=crash_data.get("proxy_ip") if hasattr(crash_data, "get") else None,
                    error=crash_data.get("error") if hasattr(crash_data, "get") else None,
                )
            except Exception:
                pass

            log_lines.append(f"    → {diag}")
            log_lines.append("")

            # If not blocked, extract and return
            if not blocked and len(html) > 500:
                extracted = extract_property_data(html, url)
                # Cross-reference with known listings
                matched = False
                for key, known in KNOWN_LISTINGS.items():
                    if url.rstrip("/") == known["url"].rstrip("/"):
                        log_lines.append(
                            f"    ⚠ Cross-check vs known: price {extracted['price_eur']} vs "
                            f"{known['expected_price']}, surface {extracted['surface_m2']} vs "
                            f"{known['expected_surface']}")
                        matched = True
                        break
                if not matched:
                    log_lines.append(
                        f"    Extracted: price={extracted['price_eur']}, "
                        f"surface={extracted['surface_m2']} m², "
                        f"DPE={extracted['dpe_energy']}, €/m²={extracted['price_per_m2']}")
                    log_lines.append(f"    Agency: {extracted['agency']}, ref: {extracted['reference']}")
                    log_lines.append("")

                return {
                    "url": url,
                    "engine": engine,
                    "status": status,
                    "blocked": False,
                    "html_length": len(html),
                    "data": extracted,
                    "raw_html": html[:5_000_000],  # cap at 5 MB
                    "diagnostics": log_lines.copy(),
                    "scan_time_s": time.time() - start,
                }

            # If blocked, accumulate and continue to next engine
            if crash_data.get("error"):
                log_lines.append(f"    Error: {crash_data['error']}")
                log_lines.append("")

            # Rate-limit breathing room between engines
            time.sleep(1.5)

        except Exception as e:
            log_lines.append(f"    Exception: {type(e).__name__}: {e}")
            log_lines.append("")
            time.sleep(1)

    # All engines blocked
    log_lines.append(f"  ⚠ ALL ENGINES BLOCKED for {url}")
    log_lines.append("")
    return {
        "url": url,
        "engine": "none",
        "status": 0,
        "blocked": True,
        "html_length": 0,
        "data": {"url": url, "scanned_at": datetime.now(timezone.utc).isoformat()},
        "raw_html": "",
        "diagnostics": log_lines,
        "scan_time_s": 0,
    }


# ---------------------------------------------------------------------------
# SCAN BATCH — multiple URLs
# ---------------------------------------------------------------------------

def scan_batch(
    urls: list[dict],
    engines: list[str] = None,
    output_dir: Path = None,
    delay_s: float = 2.0,
    stealth_use_warp: bool = True,
    camoufox_proxy: bool = False,
    jobs: int = 1,
) -> list[dict]:
    """Scan multiple URLs; urls is a list of {"name": str, "url": str} dicts.

    Returns the list of per-URL scan result dicts.  Raw HTML is written
    to disk per URL; structured data is consolidated into results.json.

    `jobs > 1` runs independent URLs concurrently with a ThreadPoolExecutor
    (review item 15).  A threading.Lock guards the shared log list.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if engines is None:
        engines = ["direct", "warp", "lightpanda", "stealth", "webshare", "webshare-stealth"]
    output_dir = Path(output_dir) if output_dir is not None else None
    if output_dir is None:
        scan_id = hashlib.md5(str(datetime.now(timezone.utc)).encode()).hexdigest()[:8]
        output_dir = SCAN_DIR / scan_id
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    log_all = [f"=== French Property Scraper — batch run "
               f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} ===", ""]
    log_lock = threading.Lock()

    def _scan_one(idx: int, entry: dict) -> dict:
        name = entry.get("name", f"listing-{idx}")
        url = entry["url"]
        local_log = []
        with log_lock:
            log_all.append(f"--- [{idx}/{len(urls)}] {name} ---")
        result = scan_url(url, engines=engines, log_lines=local_log,
                          stealth_use_warp=stealth_use_warp,
                          camoufox_proxy=camoufox_proxy)
        result["name"] = name
        with log_lock:
            log_all.extend(local_log)
            log_all.append(f"  Result: {'OK' if not result['blocked'] else 'BLOCKED'} via {result['engine']}")
            log_all.append("")
        return result

    if jobs > 1 and len(urls) > 1:
        with ThreadPoolExecutor(max_workers=min(jobs, len(urls))) as ex:
            futures = {ex.submit(_scan_one, idx, entry): idx
                       for idx, entry in enumerate(urls, 1)}
            for f in as_completed(futures):
                try:
                    results.append(f.result())
                except Exception as e:
                    results.append({"name": "error", "url": "", "engine": "",
                                    "blocked": True, "data": {},
                                    "error": str(e), "diagnostics": [], "raw_html": ""})
        # keep input order for stable reports
        by_name = {r.get("name"): r for r in results}
        results = [by_name.get(e.get("name", f"listing-{i}"), e) or e
                   for i, e in enumerate(urls, 1)]
    else:
        for idx, entry in enumerate(urls, 1):
            results.append(_scan_one(idx, entry))
            if idx < len(urls):
                time.sleep(delay_s)

    # Save raw HTML for each result (unique filenames — concurrency-safe)
    for result in results:
        if not result["blocked"] and result.get("raw_html"):
            safe_name = re.sub(r"[^\w\-]", "_", result.get("name", "listing"))
            raw_path = output_dir / f"{safe_name}.html"
            raw_path.write_text(result["raw_html"][:5_000_000], encoding="utf-8")

    # Save batch log
    log_path = output_dir / "log.txt"
    log_path.write_text("\n".join(log_all), encoding="utf-8")

    # Save consolidated JSON
    json_path = output_dir / "results.json"
    serialisable = []
    for r in results:
        copy = dict(r)
        # Strip huge raw_html from JSON
        copy.pop("raw_html", None)
        serialisable.append(copy)
    json_path.write_text(json.dumps(serialisable, indent=2, ensure_ascii=False), encoding="utf-8")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="French property scraper — multi-engine "
                    "(direct / WARP / Lightpanda / Playwright stealth / Webshare HTTP / Webshare stealth)"
    )
    parser.add_argument("--url", help="Single URL to scan")
    parser.add_argument("--engine", choices=["direct", "warp", "lightpanda", "stealth", "residential", "webshare", "webshare-stealth", "camoufox", "datadome", "cascade"],
                        default="cascade", help="Engine to use (default: cascade = all in order)")
    parser.add_argument("--input", help="File with URLs to scan (name | url per line, or JSON)")
    parser.add_argument("--output", help="Output directory override")
    parser.add_argument("--stealth-no-warp", action="store_true",
                        help="stealth engine: don't route through WARP (use direct IP)")
    parser.add_argument("--camoufox-proxy", action="store_true",
                        help="camoufox engine: route through a Webshare residential IP (sticky)")
    parser.add_argument("--jobs", type=int, default=1,
                        help="Concurrent scan workers for --input batches (default 1 = serial)")
    args = parser.parse_args()

    if not _PYTHON_SOCKS_AVAILABLE:
        print("⚠ PySocks not installed — WARP engine will be skipped. Install: pip install PySocks")

    if args.engine == "cascade":
        engines = ["direct", "warp", "lightpanda", "stealth", "webshare", "webshare-stealth"]
    else:
        engines = [args.engine]

    if args.input:
        # Read URLs from file (name | url per line, # comments ignored)
        lines = Path(args.input).read_text(encoding="utf-8").strip().splitlines()
        urls = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if " | " in line:
                name, url = line.split(" | ", 1)
                urls.append({"name": name.strip(), "url": url.strip()})
            else:
                urls.append({"name": f"line-{len(urls)+1}", "url": line})
        out_dir = Path(args.output) if args.output else None
        results = scan_batch(urls, engines=engines, output_dir=out_dir,
                             stealth_use_warp=not args.stealth_no_warp,
                             camoufox_proxy=args.camoufox_proxy,
                             jobs=args.jobs)
        print(f"\nScanned {len(results)} URLs")
        for r in results:
            status = "✅ OK" if not r["blocked"] else "❌ BLOCKED"
            print(f"  {status}  {r['name']}  |  engine={r['engine']}  |  "
                  f"price={r['data'].get('price_eur')}  |  surface={r['data'].get('surface_m2')} m²")
            print(f"         {r['url']}")
        print(f"\nResults saved to: {out_dir or 'scans/'}")

    elif args.url:
        engines = engines if args.engine != "cascade" else ["direct", "warp", "lightpanda", "stealth", "webshare", "webshare-stealth"]
        result = scan_url(args.url, engines=engines,
                          stealth_use_warp=not args.stealth_no_warp,
                          camoufox_proxy=args.camoufox_proxy,
                          explicit=(args.engine != "cascade"))
        status = "✅ OK" if not result["blocked"] else "❌ BLOCKED"
        print(f"\n{status}  engine={result['engine']}  |  {result['url']}")
        print(f"  price={result['data'].get('price_eur')}  "
              f"surface={result['data'].get('surface_m2')} m²  "
              f"DPE={result['data'].get('dpe_energy')}  €/m²={result['data'].get('price_per_m2')}")
        print(f"  agency={result['data'].get('agency')}  ref={result['data'].get('reference')}")
        print(f"\nDiagnostics:")
        for line in result["diagnostics"]:
            print(f"  {line}")
        try:
            from engine_metrics import summary as _m_summary
            ms = _m_summary(urllib.parse.urlparse(result["url"]).netloc)
            if ms and not ms.startswith("(no"):
                print(f"\nEngine metrics (domain):")
                print(ms)
        except Exception:
            pass

    else:
        # Quick demo: scan the 3 hardest-hit portals from the earlier research
        print("No URL specified. Running demo against known hard-to-scrape sites...\n")
        demo_urls = [
            {"name": "Orpi Laon 230m² (was blocked)", "url": "https://www.orpi.com/annonce-vente-immeuble-t5-laon-02000-8ab73979-6aa3-41bd-bb2f-89630451e57b/"},
            {"name": "SeLoger Aisne search (was blocked)", "url": "https://www.seloger.com/recherche/achat/immeuble/hauts-de-france/aisne-02/ad06fr2"},
            {"name": "Logic-Immo Aisne (was blocked)", "url": "https://www.logic-immo.com/recherche-immo/vente/immeuble/hauts-de-france/aisne-02/ad06fr2"},
        ]
        results = scan_batch(demo_urls, engines=engines,
                             stealth_use_warp=not args.stealth_no_warp,
                             camoufox_proxy=args.camoufox_proxy)
        print("\n--- DEMO RESULTS ---")
        for r in results:
            status = "✅ OK" if not r["blocked"] else "❌ BLOCKED"
            print(f"  {status}  {r['name']}")
            print(f"         engine={r['engine']}, price={r['data'].get('price_eur')}, "
                  f"surface={r['data'].get('surface_m2')} m²")
            print(f"         {r['url']}\n")
