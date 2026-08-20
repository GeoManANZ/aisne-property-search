"""
Central configuration for the Aisne property-search scraper suite.

SINGLE SOURCE OF TRUTH for all secrets, proxy lists, timeouts, block phrases,
UA pool, and output paths.  Every module must import from here instead of
hard-coding values.

Secrets are loaded from the gitignored `.env` file next to this module
(never committed).  See .env.example for the expected keys.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root & output paths (environment-driven — not Docker-hard-coded)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
SCAN_DIR = Path(os.environ.get(
    "HERMES_SCAN_DIR",
    PROJECT_ROOT / "scans",
))
SCAN_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Load .env (gitignored — holds secrets)
# ---------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, # comments, no fancy syntax."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv(PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
TWOCAPTCHA_API_KEY = os.environ.get("TWOCAPTCHA_API_KEY", "").strip()

# Webshare residential proxy credentials
WEBSHARE_PROXY_USER = os.environ.get("WEBSHARE_USERNAME", "").strip()
WEBSHARE_PROXY_PASS = os.environ.get("WEBSHARE_PASSWORD", "").strip()

# If env didn't provide them, fall back to the ones in proxy_config.py
# (which itself should be considered sensitive).  Keep this ONLY as a last
# resort — prefer setting WEBSHARE_USERNAME/PASSWORD in .env.
if not WEBSHARE_PROXY_USER or not WEBSHARE_PROXY_PASS:
    try:
        import sys
        sys.path.insert(0, str(Path("/workspace/hermes1/projects/nz-mortgage-saas/scrapers")))
        from proxy_config import (  # type: ignore
            WEBSHARE_PROXY_USER as _WS_USER,
            WEBSHARE_PROXY_PASS as _WS_PASS,
            WEBSHARE_PROXY_LIST as _WS_LIST,
        )
        WEBSHARE_PROXY_USER = WEBSHARE_PROXY_USER or _WS_USER
        WEBSHARE_PROXY_PASS = WEBSHARE_PROXY_PASS or _WS_PASS
        _WS_PROXY_LIST_FALLBACK = _WS_LIST
    except Exception:
        _WS_PROXY_LIST_FALLBACK = []

# Webshare static per-IP proxy list ("ip:port").  Refresh from the Webshare
# dashboard download URL when it rotates.
WEBSHARE_PROXY_LIST = [
    "31.59.20.176:6754",    # GB London
    "31.56.127.193:7684",   # US Seattle
    "45.38.107.97:6014",    # GB London
    "198.105.121.200:6462", # GB London
    "64.137.96.74:6641",    # ES Madrid
    "198.23.243.226:6361",  # US LA
    "38.154.185.97:6370",   # US Piscataway
    "84.247.60.125:6095",   # PL Warsaw
    "142.111.67.146:5611",  # JP Tokyo
    "191.96.254.138:6185",  # US LA
]
if not WEBSHARE_PROXY_LIST:
    WEBSHARE_PROXY_LIST = list(_WS_PROXY_LIST_FALLBACK)

# ---------------------------------------------------------------------------
# Proxy transports
# ---------------------------------------------------------------------------
# Cloudflare WARP SOCKS5 (docker DNS name; host uses 127.0.0.1:1080)
WARP_HOST = os.environ.get("WARP_HOST", "cloudflare-warp")
WARP_PORT = int(os.environ.get("WARP_PORT", "1080"))
WARP_SOCKS5 = f"socks5h://{WARP_HOST}:{WARP_PORT}"

# fastCRW Lightpanda browser API
FASTCRW_URL = os.environ.get("FASTCRW_URL", "http://fastcrw:3000/v1/scrape")

# A known-good Chromium binary path
CHROMIUM_PATH = os.environ.get(
    "CHROMIUM_PATH",
    "/opt/hermes/.playwright/chromium-1228/chrome-linux64/chrome",
)


# ---------------------------------------------------------------------------
# HTTP fingerprint
# ---------------------------------------------------------------------------
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

# A small UA pool for rotation (all Chrome-family, FR-first)
UA_POOL = [
    BROWSER_HEADERS["User-Agent"],
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
]

# ---------------------------------------------------------------------------
# WAF / block detection
# ---------------------------------------------------------------------------
BLOCK_PHRASES = [
    "attention required",
    "sorry, you have been blocked",
    "access denied",
    "checking your browser",
    "just a moment",
    "enable javascript and cookies",
    "are you a robot",
    "challenge-platform",
    "_cf_chl_opt",
    "ray id",
    "cf-chl-integrity",
    "performing security verification",
]

# ---------------------------------------------------------------------------
# Tuning / timeouts
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT_S = int(os.environ.get("DEFAULT_TIMEOUT_S", "20"))
CAPTCHA_MAX_SPEND_USD = float(os.environ.get("CAPTCHA_MAX_SPEND_USD", "5.0"))

# ---------------------------------------------------------------------------
# Engine cascade order (fast/cheap → heavy/expensive)
# ---------------------------------------------------------------------------
DEFAULT_ENGINE_CASCADE = [
    "direct",
    "warp",
    "lightpanda",
    "stealth",
    "webshare",
    "webshare-stealth",
]
