"""Per-account proxy support: URL validation, `requests` proxies, and a local forwarder for proxies with a password.

Chromium cannot take a username and password in `--proxy-server`, and our CDP client ignores events so it cannot
answer `Fetch.authRequired`. Instead, a proxy with credentials gets a loopback listener that adds
`Proxy-Authorization` and relays the bytes; Chromium is pointed at the listener and never sees the password.
"""
from __future__ import annotations

import base64
import select
import socket
import socketserver
import ssl
import threading
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

PROXY_ERROR = "PROXY_ERROR"
SCHEMES = ("http", "https", "socks5")


@dataclass(frozen=True)
class ProxySpec:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def has_auth(self) -> bool:
        return bool(self.username or self.password)

    @property
    def bare_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    def requests_url(self) -> str:
        # socks5h resolves names at the proxy, like Chromium does for socks5://
        scheme = "socks5h" if self.scheme == "socks5" else self.scheme
        host = f"[{self.host}]" if ":" in self.host else self.host
        auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@" if self.has_auth else ""
        return f"{scheme}://{auth}{host}:{self.port}"


def parse_proxy_url(url: str) -> ProxySpec:
    """Validates a proxy URL. Error text never repeats the input (it may hold a password)."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        raise ValueError("Địa chỉ proxy không hợp lệ") from None
    if parts.scheme not in SCHEMES:
        raise ValueError("Proxy phải bắt đầu bằng http://, https:// hoặc socks5://")
    if not parts.hostname or port is None or not 1 <= port <= 65535:
        raise ValueError("Proxy cần có host và cổng hợp lệ")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("Địa chỉ proxy không được có đường dẫn")
    spec = ProxySpec(parts.scheme, parts.hostname, port, unquote(parts.username or ""), unquote(parts.password or ""))
    if spec.scheme == "socks5" and spec.has_auth:
        raise ValueError("SOCKS5 có mật khẩu không dùng được với Chrome; hãy dùng HTTP hoặc HTTPS")
    return spec


def requests_proxies(proxy_url: str | None) -> dict | None:
    if not proxy_url:
        return None
    url = parse_proxy_url(proxy_url).requests_url()
    return {"http": url, "https": url}


def proxy_host(proxy_url: str | None) -> str:
    """host:port for display; empty when there is no (valid) proxy."""
    if not proxy_url:
        return ""
    try:
        s = parse_proxy_url(proxy_url)
    except ValueError:
        return ""
    return f"{s.host}:{s.port}"


# ---------------- liveness probe ----------------

# A neutral address for the tunnel check; no data is sent to it, only the tunnel is opened.
PROBE_TARGET = ("1.1.1.1", 443)


def probe_proxy(proxy_url: str, timeout: float = 10.0) -> str | None:
    """Asks the proxy itself to open a tunnel to a neutral address. Never contacts Muse.

    Returns None when the proxy works (reachable, login accepted, can reach out), else
    "proxy_refused" (refused or wrong login), "timeout" or "network".
    """
    spec = parse_proxy_url(proxy_url)
    try:
        if spec.scheme == "socks5":
            import socks  # PySocks

            s = socks.socksocket()
            s.set_proxy(socks.SOCKS5, spec.host, spec.port)
            s.settimeout(timeout)
            try:
                s.connect(PROBE_TARGET)
            finally:
                s.close()
            return None
        raw = socket.create_connection((spec.host, spec.port), timeout=timeout)
        conn: socket.socket = raw
        try:
            if spec.scheme == "https":
                conn = ssl.create_default_context().wrap_socket(raw, server_hostname=spec.host)
            conn.settimeout(timeout)
            lines = [f"CONNECT {PROBE_TARGET[0]}:{PROBE_TARGET[1]} HTTP/1.1", f"Host: {PROBE_TARGET[0]}:{PROBE_TARGET[1]}"]
            if spec.has_auth:
                token = base64.b64encode(f"{spec.username}:{spec.password}".encode()).decode()
                lines.append(f"Proxy-Authorization: Basic {token}")
            conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            head = b""
            while b"\r\n" not in head and len(head) < 4096:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                head += chunk
            status = head.split(b"\r\n", 1)[0].split()
            if len(status) >= 2 and status[1] == b"200":
                return None
            return "proxy_refused"
        finally:
            conn.close()
    except socket.timeout:
        return "timeout"
    except ssl.SSLError:
        return "proxy_refused"
    except Exception as exc:  # noqa: BLE001 - PySocks raises its own error types
        if type(exc).__name__ in ("ProxyConnectionError", "GeneralProxyError", "SOCKS5AuthError", "SOCKS5Error"):
            return "proxy_refused"
        return "network"


# ---------------- local forwarder ----------------

def _pipe(a: socket.socket, b: socket.socket) -> None:
    socks = [a, b]
    try:
        while True:
            ready, _, _ = select.select(socks, [], [], 120)
            if not ready:
                return
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
            # TLS sockets can hold decrypted bytes that select() cannot see
            for s in socks:
                while isinstance(s, ssl.SSLSocket) and s.pending():
                    (b if s is a else a).sendall(s.recv(s.pending()))
    except OSError:
        return


def _inject_auth(head: bytes, header: bytes, close: bool) -> bytes:
    lines = head.split(b"\r\n")
    kept = [lines[0]]
    for ln in lines[1:]:
        low = ln.lower()
        if not ln or low.startswith(b"proxy-authorization:") or (close and low.startswith(b"connection:")):
            continue
        kept.append(ln)
    kept.append(header)
    if close:
        kept.append(b"Connection: close")
    return b"\r\n".join(kept) + b"\r\n\r\n"


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        spec: ProxySpec = self.server.spec  # type: ignore[attr-defined]
        client = self.request
        client.settimeout(30)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(8192)
            if not chunk or len(buf) > 65536:
                return
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        try:
            up = socket.create_connection((spec.host, spec.port), timeout=15)
            if spec.scheme == "https":
                up = ssl.create_default_context().wrap_socket(up, server_hostname=spec.host)
        except OSError:
            return
        try:
            token = base64.b64encode(f"{spec.username}:{spec.password}".encode()).decode()
            is_connect = head.split(b" ", 1)[0].upper() == b"CONNECT"
            up.sendall(_inject_auth(head, f"Proxy-Authorization: Basic {token}".encode(), close=not is_connect) + rest)
            client.settimeout(None)
            up.settimeout(None)
            _pipe(client, up)
        finally:
            try:
                up.close()
            except OSError:
                pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, spec: ProxySpec):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.spec = spec


class TunnelPool:
    """One loopback listener per upstream proxy that has credentials. Listeners live until the process exits."""

    def __init__(self):
        self._lock = threading.Lock()
        self._servers: dict[str, _Server] = {}

    def chrome_proxy_server(self, proxy_url: str) -> str:
        """The value for Chromium's `proxyServer`: the proxy itself, or a local listener when it needs a password."""
        spec = parse_proxy_url(proxy_url)
        if not spec.has_auth:
            return spec.bare_url
        with self._lock:
            server = self._servers.get(proxy_url)
            if server is None:
                server = _Server(spec)
                threading.Thread(target=server.serve_forever, daemon=True, name="proxy-tunnel").start()
                self._servers[proxy_url] = server
            return f"http://127.0.0.1:{server.server_address[1]}"

    def close_all(self) -> None:
        with self._lock:
            servers, self._servers = list(self._servers.values()), {}
        for s in servers:
            s.shutdown()
            s.server_close()
