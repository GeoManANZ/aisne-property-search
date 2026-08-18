"""Quick unit test of the challenge classifier (no network)."""
import sys
sys.path.insert(0, "/workspace/hermes1/projects/aisne-property-search")
from importlib import util
spec = util.spec_from_file_location("s", "french-property-scraper.py")
m = util.module_from_spec(spec)
spec.loader.exec_module(m)

tests = [
    ("403", m.classify_challenge("", 403)),
    ("datadome", m.classify_challenge('<iframe src="https://geo.captcha-delivery.com">', 200)),
    ("hcaptcha", m.classify_challenge('<div class="h-captcha" data-sitekey="abc">', 200)),
    ("zilek-turnstile", m.classify_challenge("Un instant… <div id=cf-challenge>", 200)),
    ("cloudflare", m.classify_challenge("<title>Just a moment</title>", 503)),
    ("ok", m.classify_challenge("Immeuble à vendre prix 100000 m2 200 sur laon. Local commercial rdc. " * 120, 200)),
    ("superimmo-human", m.classify_challenge("Prouvez que vous êtes un humain", 200)),
]
ok = True
for name, got in tests:
    print(f"  {name:20s} → {got}")
    if got == "unknown":
        ok = False
print("\nALL CLASSIFIED:", ok)
