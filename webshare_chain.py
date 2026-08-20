"""
Webshare + WARP chained proxy transport.

WHY THIS EXISTS
  The VPS's direct route to the Webshare proxy IPs is BLOCKED at TCP level
  (connect times out to all 10 IPs, dashboard shows valid=True).  But the
  same IPs ARE reachable through the Cloudflare WARP SOCKS5 tunnel
  (cloudflare-warp:1080).  So we chain:

      VPS -> WARP SOCKS5 (cloudflare-warp:1080) -> Webshare HTTP proxy -> target

  The Webshare proxy still terminates as the residential egress IP; WARP is
  just the transport to reach it.  Verified live: through the chain,
  ipv4.webshare.io returns the Webshare proxy's own IP (31.59.20.176),
  proving the chain carries the proxy egress correctly.

USAGE
  from webshare_chain import ChainedWebshareSession
  s = ChainedWebshareSession(proxy_ip="31.59.20.176:6754")
  r = s.get("https://target/")          # r.text, r.status_code like requests
"""

import socket
import ssl
import base64
import urllib.parse

import socks  # PySocks

WARP_HOST = "cloudflare-warp"
WARP_PORT = 1080
WEBSHARE_USER = "ualfuslo"
WEBSHARE_PASS = "ukzubke2lnit"


class ChainedResponse:
    """Minimal requests-like response (status_code + text) for the chain."""

    def __init__(self, status_code: int, text: str, headers: dict):
        self.status_code = status_code
        self.text = text
        self.headers = headers


class ChainedWebshareSession:
    """requests-like session whose transport chains VPS→WARP→Webshare.

    Every request opens a fresh SOCKS5 tunnel through WARP to the Webshare
    proxy, speaks HTTP-proxy protocol over it (CONNECT for https targets,
    absolute-URI GET for http), and returns the final response.
    """

    def __init__(self, proxy_ip: str, user: str = WEBSHARE_USER,
                 passwd: str = WEBSHARE_PASS, timeout: int = 30,
                 headers: dict | None = None):
        self.proxy_ip, self.proxy_port = proxy_ip.split(":")
        self.proxy_port = int(self.proxy_port)
        self.user = user
        self.passwd = passwd
        self.timeout = timeout
        self.headers = headers or {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    def _tunnel(self) -> socket.socket:
        """Open SOCKS5 via WARP to the Webshare proxy, return the socket."""
        s = socks.socksocket()
        s.set_proxy(socks.SOCKS5, WARP_HOST, WARP_PORT)
        s.settimeout(self.timeout)
        s.connect((self.proxy_ip, self.proxy_port))
        return s

    def _proxy_auth(self) -> str:
        raw = f"{self.user}:{self.passwd}"
        return base64.b64encode(raw.encode()).decode()

    def _read_headers(self, s: socket.socket, max_bytes: int = 65536) -> tuple[dict, bytes]:
        """Read HTTP response headers + first body chunk (headers-only cap)."""
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < max_bytes:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        lines = head.decode(errors="replace").split("\r\n")
        status = int(lines[0].split(" ")[1]) if len(lines) > 0 and " " in lines[0] else 0
        hdrs = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, _, v = ln.partition(":")
                hdrs[k.strip().lower()] = v.strip()
        return status, hdrs, body

    def _body(self, s: socket.socket, hdrs: dict, first: bytes, max_bytes: int = 5_000_000) -> bytes:
        """Read the full body honouring Content-Length / chunked / close."""
        out = bytearray(first)
        cl = hdrs.get("content-length")
        if cl is not None:
            want = int(cl) - len(out)
            while want > 0 and len(out) < max_bytes:
                chunk = s.recv(min(65536, want))
                if not chunk:
                    break
                out += chunk
                want = int(cl) - len(out)
            return bytes(out)
        if hdrs.get("transfer-encoding", "").lower() == "chunked":
            while len(out) < max_bytes:
                chunk = s.recv(4096)
                if not chunk:
                    break
                out += chunk
                if b"\r\n0\r\n\r\n" in out:
                    break
            return bytes(out)
        # no length → read until close
        while len(out) < max_bytes:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def get(self, url: str, timeout: int | None = None, **kwargs) -> ChainedResponse:
        """Fetch a URL through the chained proxy.  Returns ChainedResponse."""
        if timeout is not None:
            self.timeout = timeout
        parsed = urllib.parse.urlparse(url)
        target_host = parsed.netloc
        is_https = parsed.scheme == "https"
        target_port = parsed.port or (443 if is_https else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        s = self._tunnel()
        try:
            auth = self._proxy_auth()
            if is_https:
                req = (f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                       f"Host: {target_host}:{target_port}\r\n"
                       f"Proxy-Authorization: Basic {auth}\r\n\r\n")
                s.sendall(req.encode())
                status, hdrs, body = self._read_headers(s)
                if status != 200:
                    return ChainedResponse(status, body.decode(errors="replace"), hdrs)
                # TLS over the tunnel
                ctx = ssl.create_default_context()
                tls = ctx.wrap_socket(s, server_hostname=target_host)
                head = (f"GET {path} HTTP/1.1\r\nHost: {target_host}\r\n"
                        f"Connection: close\r\n")
                for k, v in self.headers.items():
                    head += f"{k}: {v}\r\n"
                head += "\r\n"
                tls.sendall(head.encode())
                status, hdrs, body = self._read_headers(tls)
                full = self._body(tls, hdrs, body)
                tls.close()
                return ChainedResponse(status, full.decode(errors="replace"), hdrs)
            else:
                head = f"GET {url} HTTP/1.1\r\nHost: {target_host}\r\nProxy-Authorization: Basic {auth}\r\nConnection: close\r\n"
                for k, v in self.headers.items():
                    head += f"{k}: {v}\r\n"
                head += "\r\n"
                s.sendall(head.encode())
                status, hdrs, body = self._read_headers(s)
                full = self._body(s, hdrs, body)
                return ChainedResponse(status, full.decode(errors="replace"), hdrs)
        finally:
            try:
                s.close()
            except Exception:
                pass
