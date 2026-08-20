"""Quick unit test of the challenge classifier (no network)."""
import sys
sys.path.insert(0, "/workspace/hermes1/projects/aisne-property-search")
from french_property_scraper import classify_challenge

tests = [
    ("403", classify_challenge("", 403)),
    ("datadome", classify_challenge('<iframe src="https://geo.captcha-delivery.com">', 200)),
    ("hcaptcha", classify_challenge('<div class="h-captcha" data-sitekey="abc">', 200)),
    ("zilek-turnstile", classify_challenge("Un instant… <div id=cf-challenge>", 200)),
    ("cloudflare", classify_challenge("<title>Just a moment</title>", 503)),
    ("ok", classify_challenge("Immeuble à vendre prix 100000 m2 200 sur laon. Local commercial rdc. " * 120, 200)),
    ("superimmo-human", classify_challenge("Prouvez que vous êtes un humain", 200)),
]
ok = True
for name, got in tests:
    print(f"  {name:20s} → {got}")
    if got == "unknown":
        ok = False
print("\nALL CLASSIFIED:", ok)
