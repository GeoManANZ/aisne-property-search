"""
Local SOCKS5 server that chains VPS → WARP → Webshare proxy.

WHY
  Camoufox (and other browsers) need a single SOCKS5 proxy they can point at.
  The VPS cannot reach Webshare IPs directly (TCP blocked), only via the WARP
  tunnel.  This server listens on 127.0.0.1:PORT, accepts SOCKS5 CONNECT
  requests from the browser, and for each one:
    1. opens a SOCKS5 connection through WARP to the chosen Webshare IP:port
    2. performs HTTP CONNECT (or absolute-URI GET) through the Webshare proxy
       with user:pass auth
    3. splices the browser's socket to the tunnel

  Net effect: the browser's traffic egresses from the Webshare residential IP,
  with matching proxy identity for the DataDome cookie we inject.

Usage:
  python chain_socks_server.py [--port 1081] [--proxy 31.59.20.176:6754]

Browsers point at:  socks5://127.0.0.1:1081
"""
import socket
import socketserver
import threading
import argparse
import base64
import struct

import socks  # PySocks

from config import WARP_HOST, WARP_PORT, WEBSHARE_PROXY_USER, WEBSHARE_PROXY_PASS

BUFSIZE = 65536


def _splice(src, dst):
    """Bidirectionally copy bytes between two sockets until either closes."""
    try:
        while True:
            data = src.recv(BUFSIZE)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass


class ChainHandler(socketserver.BaseRequestHandler):
    """SOCKS5 server handler.  Each client connection gets its own tunnel."""

    proxy_ip = None   # set from CLI before serve_forever
    proxy_port = None
    proxy_user = WEBSHARE_PROXY_USER
    proxy_pass = WEBSHARE_PROXY_PASS

    def handle(self):
        client = self.request
        try:
            # --- SOCKS5 handshake (no auth) ---
            ver, nmethods = struct.unpack("!BB", client.recv(2))
            if ver != 5:
                return
            methods = client.recv(nmethods)
            client.sendall(b"\x05\x00")  # no-auth chosen

            # --- connect request ---
            hdr = client.recv(4)
            if len(hdr) < 4:
                return
            ver, cmd, rsv, atyp = hdr
            if cmd != 1:  # CONNECT only
                client.sendall(b"\x05\x07")
                return
            if atyp == 1:      # IPv4
                host = socket.inet_ntoa(client.recv(4))
            elif atyp == 3:    # domain
                ln = client.recv(1)[0]
                host = client.recv(ln).decode()
            elif atyp == 4:    # IPv6
                host = socket.inet_ntop(socket.AF_INET6, client.recv(16))
            else:
                client.sendall(b"\x05\x08")
                return
            port = struct.unpack("!H", client.recv(2))[0]

            # --- open WARP tunnel to the Webshare proxy ---
            up = socks.socksocket()
            up.set_proxy(socks.SOCKS5, WARP_HOST, WARP_PORT)
            up.settimeout(60)
            up.connect((self.proxy_ip, self.proxy_port))

            # --- HTTP CONNECT through Webshare to the real target ---
            basic = base64.b64encode(
                f"{self.proxy_user}:{self.proxy_pass}".encode()).decode()
            connect_req = (
                f"CONNECT {host}:{port} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Proxy-Authorization: Basic {basic}\r\n"
                f"\r\n").encode()
            up.sendall(connect_req)
            resp = up.recv(4096)
            if b" 200 " not in resp.split(b"\r\n", 1)[0]:
                client.sendall(b"\x05\x05")  # connection refused
                up.close()
                return

            # --- success reply to browser ---
            client.sendall(b"\x05\x00\x00\x01" + b"\x00" * 4 + struct.pack("!H", port))

            # --- splice both ways ---
            a = threading.Thread(target=_splice, args=(client, up), daemon=True)
            b = threading.Thread(target=_splice, args=(up, client), daemon=True)
            a.start()
            b.start()
            a.join()
            b.join()
        except Exception:
            try:
                client.close()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=1081)
    ap.add_argument("--session", default="fr-3",
                    help="Webshare rotating session (e.g. 'fr-3' = French sticky #3)")
    ap.add_argument("--proxy", default=None,
                    help="Alt: static Webshare proxy ip:port to chain through (WARP)")
    args = ap.parse_args()

    from config import WEBSHARE_ROTATE_HOSTS

    if args.proxy:
        ChainHandler.proxy_ip, ChainHandler.proxy_port = args.proxy.split(":")
        ChainHandler.proxy_port = int(ChainHandler.proxy_port)
        ChainHandler.proxy_user = WEBSHARE_PROXY_USER
        ChainHandler.proxy_pass = WEBSHARE_PROXY_PASS
        ChainHandler.use_rotate = False
    else:
        # rotating session: use the FIRST backbone host, auth with the session user
        _host = WEBSHARE_ROTATE_HOSTS[0].split(":")
        ChainHandler.proxy_ip = _host[0]
        ChainHandler.proxy_port = int(_host[1])
        _cspec = args.session.split("-") if "-" in args.session else ["fr", args.session]
        _country = _cspec[0]
        _sess = _cspec[1] if len(_cspec) > 1 else ""
        from config import webshare_rotate_url
        _url = webshare_rotate_url(
            session=int(_sess) if _sess.isdigit() else None,
            country=_country if _sess else None)
        from urllib.parse import urlparse as _up
        _p = _up(_url)
        ChainHandler.proxy_user = _p.username or ""
        ChainHandler.proxy_pass = _p.password or ""
        ChainHandler.use_rotate = True
        print(f"rotating session: {_p.username} @ {_p.hostname}:{_p.port}")

    # quick self-test of the chain before serving
    try:
        from webshare_chain import ChainedWebshareSession, RotatingWebshareSession
        if args.proxy:
            s = ChainedWebshareSession(proxy_ip=args.proxy, timeout=15)
        else:
            from config import webshare_rotate_url
            _p = None
            for host in WEBSHARE_ROTATE_HOSTS:
                try:
                    from webshare_chain import RotatingWebshareSession
                    s = RotatingWebshareSession(
                        session=int(_sess) if _sess.isdigit() else None,
                        country=_country if _sess else None,
                        timeout=15)
                    break
                except Exception:
                    continue
        r = s.get("https://ipv4.webshare.io/")
        print(f"chain self-test: egress = {r.text.strip()}")
    except Exception as e:
        print(f"⚠ chain self-test FAILED: {type(e).__name__}: {str(e)[:80]}")

    class ThreadedServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = ThreadedServer(("127.0.0.1", args.port), ChainHandler)
    print(f"chained SOCKS5 server on 127.0.0.1:{args.port} "
          f"(WARP→Webshare {args.proxy})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
