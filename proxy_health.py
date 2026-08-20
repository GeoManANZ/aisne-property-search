"""
Proxy health probing — verify each Webshare IP is reachable through the chain
and cache the healthy set, so batches skip dead proxies instead of timing out.

The VPS's direct route to Webshare IPs is blocked; the chain (VPS→WARP→
Webshare) is the working path.  Health is checked through the chain against a
cheap endpoint (ipv4.webshare.io), and the result is cached for ~10-15 min.

Use:  from proxy_health import healthy_proxies, probe_all
"""
from __future__ import annotations

import time
import random
import threading

from config import WEBSHARE_PROXY_LIST

_HEALTH_CACHE: dict[str, dict] = {}   # "ip:port" -> {"ok": bool, "at": ts}
_CACHE_TTL_S = 15 * 60                # 15 min
_LOCK = threading.Lock()


def _probe_one(proxy_ip: str, timeout: float = 8.0) -> bool:
    """Check one proxy through the chain against ipv4.webshare.io."""
    try:
        from webshare_chain import ChainedWebshareSession
        s = ChainedWebshareSession(proxy_ip=proxy_ip, timeout=timeout)
        r = s.get("https://ipv4.webshare.io/")
        ok = r.status_code == 200 and proxy_ip.split(":")[0] in r.text
        return ok
    except Exception:
        return False


def probe_all(proxy_list: list[str] | None = None,
              force: bool = False) -> list[str]:
    """Return the list of healthy "ip:port" proxies.

    Uses the cached health if fresh (< TTL).  Probes any proxy not in cache
    or expired.  Returns only the healthy ones.
    """
    proxies = proxy_list or WEBSHARE_PROXY_LIST
    now = time.time()
    healthy = []
    need_probe = []
    with _LOCK:
        for ip in proxies:
            entry = _HEALTH_CACHE.get(ip)
            if entry and not force and (now - entry["at"] < _CACHE_TTL_S):
                if entry["ok"]:
                    healthy.append(ip)
            else:
                need_probe.append(ip)
    # probe outside the lock (network I/O)
    for ip in need_probe:
        ok = _probe_one(ip)
        with _LOCK:
            _HEALTH_CACHE[ip] = {"ok": ok, "at": time.time()}
        if ok:
            healthy.append(ip)
        print(f"  [proxy-health] {ip} → {'healthy' if ok else 'DEAD'}")
    return healthy


def healthy_proxies(proxy_list: list[str] | None = None,
                    force: bool = False) -> list[str]:
    """Convenience: healthy proxies, cached.  Use this in engines."""
    return probe_all(proxy_list, force=force)


def pick_healthy(proxy_list: list[str] | None = None) -> str | None:
    """Pick one random healthy proxy, or None if none are healthy."""
    healthy = healthy_proxies(proxy_list)
    if not healthy:
        return None
    # prefer a proxy not recently used (simple round-robin by avoiding the
    # first one from a shuffled copy)
    return random.choice(healthy)


if __name__ == "__main__":
    print("probing all proxies through the chain...")
    ok = healthy_proxies(force=True)
    print(f"\nhealthy: {len(ok)}/{len(WEBSHARE_PROXY_LIST)}")
    for ip in WEBSHARE_PROXY_LIST:
        entry = _HEALTH_CACHE.get(ip)
        print(f"  {ip} → {'✓' if entry and entry['ok'] else '✗'}")
