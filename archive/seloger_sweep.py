"""Resilient SeLoger pagination sweep — cycles French sticky sessions until one
yields a solveable DataDome challenge, then clicks through all SERP pages.

WHY SESSION CYCLING
  Webshare rotating-plan sticky sessions (ualfuslo-fr-N) rotate their
  underlying IP over time.  Some French residential IPs are flagged by
  DataDome (2Captcha returns ERROR_CAPTCHA_UNSOLVABLE); others are clean.
  We cycle sessions 1..12 until we find one that (a) solves, and (b) the
  refetch with the solved cookie returns real listings.  That session is then
  kept for the Camoufox browser session (via the chain SOCKS server) so the
  browser egresses from the exact IP the cookie is bound to.

Usage:
  python seloger_sweep.py [--max-pages 9] [--sessions 12]

Requires chain_socks_server.py --session fr-<N> running on 127.0.0.1:1081 for
the SAME <N> that wins the solve.
"""
import sys, re, time, json, subprocess, socket, argparse
from pathlib import Path
sys.path.insert(0, Path(__file__).parent.as_posix())

from twocaptcha_client import solve_datadome, get_balance
from config import build_fingerprint
from french_property_scraper import _parse_datadome_dd, _build_datadome_challenge_url
from french_property_parsers import parse_seloger
from webshare_chain import RotatingWebshareSession

UA = build_fingerprint()["user_agent"]
SCROLL_PASSES = 8
SCROLL_WAIT_S = 0.8
PAGE_SETTLE_S = 4.0


def solve_for_session(url, session, country="fr"):
    """Try to solve DataDome for one session.  Returns (cookie, ip) or (None, ip)."""
    try:
        s = RotatingWebshareSession(session=session, timeout=45, country=country,
                                    headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
        r = s.get(url)
        dd = _parse_datadome_dd(r.text)
        if not dd:
            return None, None
        cookie_set = solve_datadome(captcha_url=_build_datadome_challenge_url(dd, url),
                                    page_url=url, user_agent=UA,
                                    proxy=s.proxy_auth_str(), proxytype="socks5")
        mm = re.search(r"datadome=([^;]+)", cookie_set or "")
        if not mm:
            return None, None
        # verify the cookie works: refetch through the same session
        s2 = RotatingWebshareSession(session=session, timeout=45, country=country,
                                     headers={"User-Agent": UA,
                                              "Accept-Language": "fr-FR,fr;q=0.9",
                                              "Cookie": f"datadome={mm.group(1)}"})
        r2 = s2.get(url)
        ok = "annonce" in r2.text.lower() and len(r2.text) > 3000
        if ok:
            return mm.group(1), r2.text
        return None, None
    except Exception:
        return None, None


def chain_ip():
    """Egress IP of the local chain SOCKS server (127.0.0.1:1081)."""
    try:
        import socks
        ts = socks.socksocket()
        ts.set_proxy(socks.SOCKS5, "127.0.0.1", 1081)
        ts.settimeout(15)
        ts.connect(("ipv4.webshare.io", 80))
        ts.sendall(b"GET / HTTP/1.0\r\nHost: ipv4.webshare.io\r\n\r\n")
        data = ts.recv(4096).decode(errors="replace")
        ts.close()
        return data.split("\r\n\r\n")[-1].strip()
    except Exception:
        return None


def click_next(page) -> bool:
    try:
        page.evaluate("""() => {
            const b = document.querySelector('button[aria-label="page suivante"]');
            if (b) b.scrollIntoView({block: 'center'});
        }""")
        page.wait_for_timeout(800)
        pos = page.evaluate("""() => {
            const b = document.querySelector('button[aria-label="page suivante"]');
            if (!b) return null;
            const r = b.getBoundingClientRect();
            return {x: r.x + r.width/2, y: r.y + r.height/2, inView: r.top>=0 && r.bottom<=innerHeight};
        }""")
        if pos and pos['inView']:
            page.mouse.click(pos['x'], pos['y'])
            return True
    except Exception:
        pass
    try:
        btn = page.locator('button[aria-label="page suivante"]').first
        btn.click(timeout=5000, force=True)
        return True
    except Exception:
        return False


def current_page_num(page) -> int:
    try:
        cur = page.locator('button[aria-label*="page actuelle"]').first
        if cur.is_visible(timeout=2000):
            return int(cur.text_content().strip())
    except Exception:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://www.seloger.com/recherche/achat/immeuble/hauts-de-france/aisne-02/ad06fr2")
    ap.add_argument("--outdir", default="seloger_pages")
    ap.add_argument("--max-pages", type=int, default=9)
    ap.add_argument("--sessions", type=int, default=12, help="French sessions to try (fr-1..fr-N)")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)
    print(f"balance before: {get_balance()}")
    print(f"chain server egress: {chain_ip()}")

    # Phase 1: find a session that solves
    cookie, winning_session = None, None
    for sess in range(1, args.sessions + 1):
        print(f"trying fr-{sess} ...", flush=True)
        c, html = solve_for_session(args.url, sess)
        if c:
            cookie, winning_session = c, sess
            print(f"  ✓ fr-{sess} solved + verified!", flush=True)
            break
        print(f"  ✗ fr-{sess} (solve failed / IP flagged)", flush=True)
    if not cookie:
        print("NO SESSION SOLVED — aborting")
        sys.exit(1)

    # Phase 2: restart the chain server pinned to the winning session
    print(f"restarting chain server → fr-{winning_session} ...")
    subprocess.run(["pkill", "-f", "chain_socks_server.py"], capture_output=True)
    time.sleep(1)
    chain_proc = subprocess.Popen(
        [sys.executable, "chain_socks_server.py", "--session", f"fr-{winning_session}"],
        cwd=Path(__file__).parent,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    cip = chain_ip()
    print(f"chain server egress now: {cip}")

    # Phase 3: browser click-through
    from camoufox.sync_api import Camoufox
    launch = {"headless": True, "locale": "fr-FR", "humanize": True,
              "geoip": False,
              "proxy": {"server": "socks5://127.0.0.1:1081"}}

    all_listings, seen_urls = [], set()
    with Camoufox(**launch) as browser:
        ctx = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris",
                                  viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        ctx.add_cookies([{"name": "datadome", "value": cookie,
                          "domain": ".seloger.com", "path": "/", "secure": True}])
        print("injected cookie")
        for attempt in range(3):
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
                break
            except Exception as e:
                print(f"  goto {attempt+1}: {type(e).__name__}")
                time.sleep(4)
        page.wait_for_timeout(int(PAGE_SETTLE_S * 1000))

        last_pn = 0
        for page_idx in range(1, args.max_pages + 1):
            for _ in range(SCROLL_PASSES):
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(int(SCROLL_WAIT_S * 1000))
            html = page.content()
            (outdir / f"page_{page_idx:02d}.html").write_text(html, encoding="utf-8")
            listings = parse_seloger(html, args.url)
            new = 0
            for l in listings:
                if l["url"] not in seen_urls:
                    seen_urls.add(l["url"])
                    all_listings.append(l)
                    new += 1
            pn = current_page_num(page)
            print(f"  page {page_idx}: {len(listings)} cards ({new} new) — total {len(all_listings)}", flush=True)
            if not listings:
                print("  no cards — stopping")
                break
            if page_idx >= args.max_pages:
                break
            if not click_next(page):
                print("  no next button — done")
                break
            # poll for page advance + cards
            advanced = False
            for _ in range(30):
                page.wait_for_timeout(1000)
                pn_now = current_page_num(page)
                ncards = page.evaluate("""() => document.querySelectorAll('[data-testid^="classified-card-mfe-"]').length""")
                if pn_now > pn and ncards > 0:
                    advanced = True
                    break
            if not advanced:
                print(f"  page stuck at {pn} — stopping")
                break

    out = outdir / "all_listings.json"
    out.write_text(json.dumps(all_listings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {len(all_listings)} unique listings → {out}")
    print(f"balance after: {get_balance()}")
    # cleanup chain server
    chain_proc.terminate()


if __name__ == "__main__":
    main()
