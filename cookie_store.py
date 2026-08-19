"""
Cookie persistence store — reuse successful anti-bot cookies across runs.

The costly part of a scrape isn't the fetch, it's the CHALLENGE: DataDome
and Cloudflare issue a `datadome` / `cf_clearance` cookie when the challenge
passes, and then honour it for a while (minutes to hours).  If we save the
cookie each time an engine succeeds, the next run for the SAME (proxy IP,
domain) pair can load it straight into the browser context and skip the
challenge entirely — far faster and less likely to re-trigger.

Storage: one JSON file (cookies.json) mapping "ip|domain" → [cookie dicts].

Security note: cf_clearance / datadome cookies are session-scoped to an IP
and domain; they contain no secrets and expire quickly.  Storing them in a
local JSON is acceptable.  We key by the proxy IP because the cookie is tied
to the IP that earned it — reusing a cookie on a different IP usually fails.
"""

import json
import time
from pathlib import Path

_COOKIE_FILE = Path(__file__).parent / "cookies.json"
_TTL_S = 60 * 60 * 6  # 6 hours — challenge cookies rarely last longer


def _load() -> dict:
    try:
        data = json.loads(_COOKIE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict):
    _COOKIE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _key(ip: str, domain: str) -> str:
    return f"{ip}|{domain}"


def get_cookies(ip: str, domain: str) -> list[dict]:
    """Return saved cookies for (ip, domain), or [] if none/fresh enough."""
    if not ip:
        return []
    data = _load()
    entry = data.get(_key(ip, domain))
    if not entry:
        return []
    # check TTL
    if time.time() - entry.get("saved_at", 0) > _TTL_S:
        return []
    return entry.get("cookies", [])


def save_cookies(ip: str, domain: str, cookies: list[dict]):
    """Persist cookies for (ip, domain) with a timestamp."""
    if not ip or not cookies:
        return
    data = _load()
    data[_key(ip, domain)] = {
        "saved_at": time.time(),
        "cookies": cookies,
    }
    # prune very old entries so the file stays small
    now = time.time()
    for k in list(data):
        if now - data[k].get("saved_at", 0) > _TTL_S:
            del data[k]
    _save(data)


def clear(ip: str | None = None, domain: str | None = None):
    """Remove cookies, optionally filtered by ip and/or domain."""
    data = _load()
    if ip is None and domain is None:
        _save({})
        return
    for k in list(data):
        k_ip, k_dom = k.split("|", 1)
        if ip and k_ip == ip:
            del data[k]
        elif domain and k_dom == domain:
            del data[k]
    _save(data)
