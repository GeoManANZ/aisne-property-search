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
# Search area — departments to scrape
# ---------------------------------------------------------------------------
# Each entry: place_id (SeLoger BFF location id), slug (URL path segment),
# label.  To extend to other departments later, add entries, e.g.:
#   {"place_id": "AD80FR2", "slug": "somme-80", "label": "Somme"},
DEPARTMENTS = [
    {"place_id": "AD06FR2", "slug": "aisne-02", "label": "Aisne (02)"},
]
DEFAULT_DEPARTMENT = DEPARTMENTS[0]

# ---------------------------------------------------------------------------
# Investment criteria — the report/alert filter layer
# ---------------------------------------------------------------------------
# Used by recommendations_report.py (candidate selection) and any ranking.
# Amend here; every consumer picks it up automatically.
INVESTMENT = {
    "min_surface_m2": 150,      # minimum habitable surface
    "max_surface_m2": 1500,     # ceiling — kills land-parcel mis-parses.
                                # Audit 2026-08-26: every real building in DB
                                # is <=1000 m²; every row >2000 m² is terrain/
                                # forest/étangs (96,594 m² "Domaine forestier",
                                # 18,073 m² "deux étangs", 15,912 m² "hutte de
                                # chasse", 4,106 m² "terrain de loisir").
                                # lesiteimmo labels land as "maison" and puts
                                # land area in "surface habitable".
    "price_min_eur": 10_000,    # ignore below (junk/parts listings)
    "price_max_eur": 220_000,   # budget ceiling
    "rank_by": "price_per_m2",  # 'price_per_m2' (asc) is the only mode for now
    "top_n": 20,                # default report size
}

# ---------------------------------------------------------------------------
# Price-alert guards
# ---------------------------------------------------------------------------
# A drop is reported only if it passes ALL of these (false-positive defence,
# see LEARNINGS.md §9):
PRICE_ALERT = {
    "days_window": 30,          # compare against prices seen in the last N days
    "min_pct": 0.0,             # minimum drop % to report (0 = any real drop)
    "price_floor_eur": 10_000,  # implausible-below guard
    "price_ceiling_eur": 50_000_000,  # malformed-above guard
}


def seloger_serp_url(dep: dict | None = None) -> str:
    """SeLoger SERP URL for a department."""
    d = dep or DEFAULT_DEPARTMENT
    return (f"https://www.seloger.com/recherche/achat/immeuble/"
            f"hauts-de-france/{d['slug']}/{d['place_id'].lower()}")


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

# Fail loudly if credentials are missing — the nz-mortgage-saas proxy_config
# fallback was removed (it broke outside the Hermes1 layout).  Set
# WEBSHARE_USERNAME / WEBSHARE_PASSWORD in .env.
if not WEBSHARE_PROXY_USER or not WEBSHARE_PROXY_PASS:
    raise RuntimeError(
        "Missing Webshare credentials: set WEBSHARE_USERNAME and "
        "WEBSHARE_PASSWORD in the project .env (see .env.example)."
    )

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

# French sticky-session username prefix (used by seloger_api_sweep.py).
# Full username = f"{WEBSHARE_FR_STICKY_PREFIX}-{session}" (e.g. ualfuslo-fr-1).
WEBSHARE_FR_STICKY_PREFIX = os.environ.get("WEBSHARE_FR_STICKY_PREFIX", "ualfuslo-fr")

# ---------------------------------------------------------------------------
# Webshare ROTATING residential plan (backbone, added 2026-08-20)
# ---------------------------------------------------------------------------
# Backbone = p.webshare.io:80 (hostname may not resolve from Docker DNS — use
# the DoH-resolved IPs: 185.24.10.165, 104.36.49.13, 91.242.215.155, ...).
# Username suffix is the STICKY SESSION:
#   - no suffix (ualfuslo-rotate)  → rotate IP per request
#   - numbered   (ualfuslo-1)      → same residential IP every request
# The datadome cookie is IP-bound, so solve + refetch MUST use the same
# numbered session (e.g. ualfuslo-7 for both steps).
WEBSHARE_ROTATE_HOSTS = [
    "185.24.10.165:80",   # backbone anycast (ap-southeast-2)
    "104.36.49.13:80",
    "91.242.215.155:80",
]
WEBSHARE_ROTATE_USER = os.environ.get("WEBSHARE_ROTATE_USER", "ualfuslo-rotate")
# numbered sticky sessions use base username WITHOUT the -rotate suffix:
# the download list gives ualfuslo-1 .. ualfuslo-<N>, not ualfuslo-rotate-N.
WEBSHARE_ROTATE_BASE_USER = os.environ.get("WEBSHARE_ROTATE_BASE_USER", "ualfuslo")
WEBSHARE_ROTATE_SESSION = os.environ.get("WEBSHARE_ROTATE_SESSION", "1")
WEBSHARE_ROTATE_URL = f"http://{WEBSHARE_ROTATE_BASE_USER}-{WEBSHARE_ROTATE_SESSION}:{WEBSHARE_PROXY_PASS}@{WEBSHARE_ROTATE_HOSTS[0]}"
# number of numbered sticky sessions the plan provides (from the download list)
WEBSHARE_ROTATE_SESSIONS = 20


def webshare_rotate_url(session: int | None = None, host_idx: int = 0,
                        country: str | None = None) -> str:
    """Build a rotating-plan proxy URL.

    session=None → per-request rotation (ualfuslo-rotate)
    session=N   → sticky residential IP (ualfuslo-N)
    country=fr  → French residential IPs (ualfuslo-fr, rotates within FR)
    BOTH        → French AND sticky (ualfuslo-fr-1) — best for DataDome
    """
    host = WEBSHARE_ROTATE_HOSTS[host_idx % len(WEBSHARE_ROTATE_HOSTS)]
    if session is None and not country:
        user = WEBSHARE_ROTATE_USER          # ualfuslo-rotate (rotates)
    elif session is None:
        user = f"{WEBSHARE_ROTATE_BASE_USER}-{country}"  # ualfuslo-fr
    elif country:
        user = f"{WEBSHARE_ROTATE_BASE_USER}-{country}-{session}"  # ualfuslo-fr-1
    else:
        user = f"{WEBSHARE_ROTATE_BASE_USER}-{session}"  # ualfuslo-N (sticky)
    return f"http://{user}:{WEBSHARE_PROXY_PASS}@{host}"

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
# Fingerprint consistency (review item 16)
# ---------------------------------------------------------------------------
# A single "fingerprint" = (UA, Accept-Language, viewport, timezone, locale).
# It must be IDENTICAL across solve → browser → subsequent requests for the
# same target, or DataDome/Cloudflare re-challenge (IP-bound cookies are
# checked against the client fingerprint too).
def build_fingerprint(ua: str | None = None) -> dict:
    """Return a full fingerprint dict.  All engines should call this once per
    target URL and thread the SAME fingerprint through every step."""
    if ua is None:
        import random
        ua = random.choice(UA_POOL)
    return {
        "user_agent": ua,
        "accept_language": "fr-FR,fr;q=0.9,en;q=0.8",
        "viewport": {"width": 1440, "height": 900},
        "timezone_id": "Europe/Paris",
        "locale": "fr-FR",
    }

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
