#!/usr/bin/env python3
"""Recon century21.fr dept listing pages: card structure, price/surface fields, pagination.

Direct egress only — no proxy, no captcha service. Short, polite, one request per URL.
Saves the raw HTML so parser work can be done offline against a real page.
"""
import pathlib
import re
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0 Safari/537.36")
ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "scans" / "century21_probe"
OUT.mkdir(parents=True, exist_ok=True)

URLS = {
    "maison_aisne": "https://www.century21.fr/annonces/achat-maison/d-02_aisne/",
    "appart_aisne": "https://www.century21.fr/annonces/achat-appartement/d-02_aisne/",
    "immeuble_aisne": "https://www.century21.fr/annonces/achat-immeuble/d-02_aisne/",
}


def get(url: str) -> tuple[str, int]:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "fr-FR,fr;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace"), r.status


for name, url in URLS.items():
    body = ""
    try:
        body, status = get(url)
    except Exception as e:                                   # noqa: BLE001
        print(f"{name}: FETCH FAILED {type(e).__name__}: {str(e)[:110]}")
        continue
    p = OUT / f"{name}.html"
    p.write_text(body, encoding="utf-8")
    print(f"\n=== {name}: http={status} bytes={len(body)} -> {p.name}")
    for pat, label in [
        (r"(\d{1,3}(?:[\s\u00a0\u202f]\d{3})+)\s*€", "price spaced"),
        (r"(\d{2,7})\s*€", "price plain"),
        (r"(\d{2,4})\s*m²", "surface m2"),
        (r"/trouver_logement/detail/\d+", "detail links"),
    ]:
        m = re.findall(pat, body)
        print(f"   {label:14s}: {len(m):4d}  e.g. {m[:4]}")
    res = re.findall(r"(\d{2,5})\s*(?:annonces|résultats|biens)", body)
    print(f"   result count  : {res[:3]}")

i = body.find("/trouver_logement/detail/")
if i > 0:
    print("\n--- CONTEXT AROUND FIRST DETAIL LINK (card wrapper) ---")
    print(body[max(0, i - 1800):i + 1200])
