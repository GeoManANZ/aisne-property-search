"""
Engine metrics + adaptive routing store.

Records every engine outcome (success/failure, block reason, timing) in a
small JSONL file and exposes the historical best engine per (domain,
challenge_type) so early routing can prefer what actually works — instead of
a static cascade order.

Files:
  engine_metrics.jsonl — append-only, one JSON object per scan attempt.

Usage:
  from engine_metrics import record_outcome, best_engine_for

  record_outcome(domain="www.seloger.com", challenge="datadome",
                 engine="datadome", success=True, timing_s=12.4,
                 proxy_ip="31.59.20.176", error=None)

  engines = best_engine_for("www.seloger.com", "datadome")  # e.g. ["datadome", "camoufox", ...]
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_METRICS_FILE = Path(__file__).parent / "engine_metrics.jsonl"
_MAX_LINES = 5000          # cap file size; drop oldest beyond this

# Static default order per challenge type (used before history builds up).
_DEFAULT_ORDER = {
    "datadome":   ["datadome", "camoufox", "webshare-stealth", "stealth"],
    "hcaptcha":   ["camoufox", "webshare-stealth", "stealth"],
    "turnstile":  ["camoufox", "webshare-stealth", "stealth"],
    "cloudflare": ["stealth", "webshare", "webshare-stealth", "camoufox"],
    "hard403":    ["webshare", "webshare-stealth", "camoufox"],
    "ok":         [],
    "unknown":    ["webshare", "stealth", "camoufox"],
}


def _read_lines() -> list[dict]:
    if not _METRICS_FILE.exists():
        return []
    out = []
    try:
        for line in _METRICS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out


def record_outcome(domain: str, challenge: str, engine: str, success: bool,
                   timing_s: float = 0.0, proxy_ip: str | None = None,
                   error: str | None = None) -> None:
    """Append one engine outcome to the metrics log."""
    entry = {
        "ts": time.time(),
        "domain": domain,
        "challenge": challenge,
        "engine": engine,
        "success": bool(success),
        "timing_s": round(float(timing_s or 0), 2),
        "proxy_ip": proxy_ip,
        "error": (error or "")[:200],
    }
    try:
        lines = _read_lines()
        lines.append(entry)
        if len(lines) > _MAX_LINES:
            lines = lines[-_MAX_LINES:]
        with _METRICS_FILE.open("w", encoding="utf-8") as f:
            for ln in lines:
                f.write(json.dumps(ln, ensure_ascii=False) + "\n")
    except Exception:
        pass


def best_engine_for(domain: str, challenge: str,
                    history_window_s: float = 7 * 86400) -> list[str]:
    """Return engines ordered by historical success rate for this pair.

    Uses the last N attempts within the window; falls back to the static
    default order when there is no history (or history is too thin).
    """
    lines = _read_lines()
    now = time.time()
    # aggregate per engine: successes / attempts, recency-weighted
    stats: dict[str, dict] = {}
    for e in lines:
        if e.get("domain") != domain or e.get("challenge") != challenge:
            continue
        age = now - e.get("ts", 0)
        if age > history_window_s:
            continue
        eng = e.get("engine", "")
        if not eng:
            continue
        st = stats.setdefault(eng, {"ok": 0, "n": 0, "sum_t": 0.0})
        st["n"] += 1
        st["sum_t"] += float(e.get("timing_s", 0) or 0)
        if e.get("success"):
            st["ok"] += 1

    defaults = _DEFAULT_ORDER.get(challenge, _DEFAULT_ORDER["unknown"])
    if not stats:
        return list(defaults)

    def score(eng: str) -> tuple:
        st = stats.get(eng)
        if not st or st["n"] < 2:
            # not enough data → keep static position, after proven engines
            return (0, 0, defaults.index(eng) if eng in defaults else 99)
        rate = st["ok"] / st["n"]
        return (rate, st["n"], -st["sum_t"] / st["n"])

    ordered = sorted(defaults, key=score, reverse=True)
    # append any engine with history but not in defaults
    for eng in stats:
        if eng not in ordered:
            ordered.append(eng)
    return ordered


def summary(domain: str | None = None) -> str:
    """Human-readable metrics summary for diagnostics."""
    lines = _read_lines()
    if not lines:
        return "(no engine metrics yet)"
    out = []
    by = {}
    for e in lines:
        if domain and e.get("domain") != domain:
            continue
        key = (e.get("domain"), e.get("challenge"), e.get("engine"))
        st = by.setdefault(key, {"ok": 0, "n": 0})
        st["n"] += 1
        if e.get("success"):
            st["ok"] += 1
    for (dom, chal, eng), st in sorted(by.items()):
        out.append(f"  {dom} | {chal:10s} | {eng:15s} | {st['ok']}/{st['n']} "
                   f"({100*st['ok']/st['n']:.0f}%)")
    return "\n".join(out) if out else "(no matches)"


if __name__ == "__main__":
    print("engine metrics file:", _METRICS_FILE)
    print(summary())
