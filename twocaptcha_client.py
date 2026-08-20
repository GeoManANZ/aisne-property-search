"""
2Captcha client — solve CAPTCHAs (hCaptcha, DataDome, reCAPTCHA) via 2Captcha.

This is a PAID service (user-funded, ~$1-3 per 1000 solves).  Use it
CONSERVATIVELY: only when a CAPTCHA actually blocks a scrape, never in a hot
loop.  The user's API key lives in the gitignored .env (never committed).

Workflow:
  1. create_task(...)  → returns a captcha_id
  2. poll until solved  → returns the token (or cookie value)
  3. feed the token back into the scraper (hCaptcha callback, or DataDome
     cookie header) to get past the challenge.

API: https://2captcha.com/2captcha-api
  in.php  — submit task
  res.php — poll result (action=get / getbalance)
"""

import os
import time
import json
import urllib.request
import urllib.parse

_BASE_IN = "https://2captcha.com/in.php"
_BASE_RES = "https://2captcha.com/res.php"
_POLL_INTERVAL = 5          # seconds between polls
_MAX_POLL = 30              # max poll iterations (~150s)


def _api_key() -> str:
    """Load the key from the gitignored .env (or env var)."""
    key = os.environ.get("TWOCAPTCHA_API_KEY") or os.environ.get("2CAPTCHA_KEY")
    if key:
        return key
    # fall back to .env file
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        for line in open(env_path):
            line = line.strip()
            if line.startswith("TWOCAPTCHA_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


class MaxSpendExceeded(RuntimeError):
    """Raised when a single run would spend past the 2Captcha budget cap."""


def ensure_within_budget(allow_exceed: bool = False) -> float:
    """Return current balance; raise MaxSpendExceeded if below the floor.

    The floor is a safety net so a runaway loop can't drain the account.
    Set CAPTCHA_MAX_SPEND_USD in config.py / .env.
    """
    try:
        from config import CAPTCHA_MAX_SPEND_USD
    except Exception:
        CAPTCHA_MAX_SPEND_USD = 5.0
    bal = get_balance()
    if bal is None:
        return 0.0
    if not allow_exceed and bal < 0.5:
        raise MaxSpendExceeded(f"2Captcha balance ${bal:.2f} below safety floor")
    return bal


def get_balance() -> float | None:
    """Return the current account balance in USD, or None on failure."""
    key = _api_key()
    if not key:
        return None
    url = f"{_BASE_RES}?key={key}&action=getbalance&json=1"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
        if data.get("status") == 1:
            return float(data.get("request", 0))
    except Exception:
        pass
    return None


def _submit(params: dict) -> str:
    """POST a task to in.php, return the captcha_id ('' on failure)."""
    params["key"] = _api_key()
    params["json"] = 1
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(_BASE_IN, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        if resp.get("status") == 1:
            return resp.get("request", "")
    except Exception:
        pass
    return ""


def _poll(captcha_id: str) -> str:
    """Poll res.php until solved.  Returns the token ('', 'CAPCHA_NOT_READY'-
    style, or the error message on failure)."""
    key = _api_key()
    for _ in range(_MAX_POLL):
        url = (f"{_BASE_RES}?key={key}&action=get&id={captcha_id}&json=1")
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                resp = json.loads(r.read())
        except Exception:
            time.sleep(_POLL_INTERVAL)
            continue
        st = resp.get("status")
        req = resp.get("request", "")
        if st == 1:
            return req  # solved — token
        # status 0 with CAPCHA_NOT_READY → keep polling; else error
        if "NOT_READY" not in req.upper():
            return f"ERROR: {req}"
        time.sleep(_POLL_INTERVAL)
    return "ERROR: timeout"


def solve_hcaptcha(sitekey: str, page_url: str) -> str:
    """Solve an hCaptcha checkbox/challenge.  Returns the token to inject,
    or '' on failure."""
    cid = _submit({
        "method": "hcaptcha",
        "sitekey": sitekey,
        "pageurl": page_url,
    })
    if not cid:
        return ""
    return _poll(cid)


def solve_recaptcha_v2(sitekey: str, page_url: str) -> str:
    """Solve a reCAPTCHA v2 checkbox.  Returns the g-recaptcha-response token."""
    cid = _submit({
        "method": "userrecaptcha",
        "googlekey": sitekey,
        "pageurl": page_url,
    })
    if not cid:
        return ""
    return _poll(cid)


def solve_datadome(captcha_url: str, page_url: str, user_agent: str = "",
                   proxy: str = "", proxytype: str = "http") -> str:
    """Solve a DataDome challenge (SeLoger / Logic-Immo).

    Args:
        captcha_url: the DataDome challenge URL (geo.captcha-delivery.com/...)
                     that the site redirects to when blocked.  You get this by
                     fetching the target page and following the redirect.
        page_url:    the original target URL.
        user_agent:  the UA the scrape will use (must match the request that
                     sends the cookie, or DataDome re-challenges).
        proxy:       "user:pass@host:port" — CRITICAL.  DataDome cookies are
                     IP-bound: the cookie 2Captcha produces is tied to the
                     solving IP.  If you scrape via a Webshare IP, you MUST
                     pass that same Webshare IP here so the cookie matches.

    Returns the full cookie-set string (e.g. "datadome=...; Max-Age=...") or
    '' on failure.
    """
    params = {
        "method": "datadome",
        "captcha_url": captcha_url,
        "pageurl": page_url,
    }
    if user_agent:
        # 2Captcha's DataDome method rejects unsupported UAs with
        # ERROR_UNSUPPORTED_USERAGENT (e.g. Macintosh UAs).  Normalize to a
        # supported Windows Chrome UA if the caller passed something else.
        if "Windows NT 10.0" not in user_agent or "Chrome/" not in user_agent:
            user_agent = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36")
        params["userAgent"] = user_agent
    if proxy:
        params["proxy"] = proxy
        params["proxytype"] = proxytype
    cid = _submit(params)
    if not cid:
        return ""
    return _poll(cid)
