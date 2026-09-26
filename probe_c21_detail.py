#!/usr/bin/env python3
"""Check which description source a century21.fr DETAIL page exposes.

Direct fetch, free. Mirrors fill_descriptions._pick's candidate order so the
answer tells us whether adding 'century21' to DETAIL_SOURCES will work.
"""
import html
import re
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

URL = "https://www.century21.fr/trouver_logement/detail/15170646251/"

req = urllib.request.Request(URL, headers={"User-Agent": UA,
                                           "Accept-Language": "fr-FR,fr;q=0.9"})
body = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
print(f"fetched {len(body)} bytes from {URL}\n")


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


CANDIDATES = [
    ("JSON-LD description", r'"description"\s*:\s*"([^"]{40,})"'),
    ("og:description", r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']{40,})'),
    ("meta description", r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{40,})'),
    ("div class~description", r'class="[^"]*[Dd]escription[^"]*"[^>]*>(.{40,800}?)</div>'),
    ("section Description", r'id="description"[^>]*>(.{40,900}?)</(?:section|div)>'),
]

for label, pat in CANDIDATES:
    m = re.search(pat, body, re.S)
    if m:
        val = strip_tags(html.unescape(m.group(1)))
        print(f"  {label:22s} HIT  ({len(val)} chars)")
        print(f"      {val[:260]}")
    else:
        print(f"  {label:22s} no match")

# Also: does the page carry the works/devises block and the DPE we care about?
for label, pat in (("Travaux à prévoir", r"Travaux\s*à\s*prévoir(.{0,200})"),
                   ("DPE mention", r"(?:Non soumis au DPE|Classe énergie|DPE\s*:?\s*[A-G]\b)")):
    m = re.search(pat, body, re.S)
    print(f"  {label:22s} {'HIT: ' + strip_tags(m.group(0))[:120] if m else 'no match'}")
