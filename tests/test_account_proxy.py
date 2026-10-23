"""Offline checks: per-account proxy validation, the password forwarder, fail-closed behaviour, and secret hiding."""
import base64
from pathlib import Path
import socket
import socketserver
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as engine_module  # noqa: E402
import proxy_support  # noqa: E402
from engine import MuseProxyError  # noqa: E402
from proxy_support import TunnelPool, parse_proxy_url, probe_proxy, requests_proxies  # noqa: E402
from store import Store  # noqa: E402


def test_parse_accepts_supported_schemes():
    s = parse_proxy_url("http://user:p%40ss@10.0.0.1:8080")
    assert (s.scheme, s.host, s.port, s.username, s.password) == ("http", "10.0.0.1", 8080, "user", "p@ss")
    assert parse_proxy_url("socks5://1.2.3.4:1080").bare_url == "socks5://1.2.3.4:1080"
    assert parse_proxy_url("https://h.example:443").scheme == "https"


@pytest.mark.parametrize("bad", ["", "ftp://h:1", "http://h", "http://:80", "http://h:0", "http://h:70000",
                                 "http://h:80/path", "socks5://u:SECRETPW@h:1080"])
def test_parse_rejects_bad_urls_without_echoing_secret(bad):
    with pytest.raises(ValueError) as e:
        parse_proxy_url(bad)
    assert "SECRETPW" not in str(e.value)


def test_requests_proxies_resolves_names_at_socks_proxy():
    assert requests_proxies(None) is None
    assert requests_proxies("socks5://h:1080") == {"http": "socks5h://h:1080", "https": "socks5h://h:1080"}
    assert requests_proxies("http://u:p@h:80")["https"] == "http://u:p@h:80"


class _Upstream(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _fake_proxy(heads):
    class H(socketserver.BaseRequestHandler):
        def handle(self):
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += self.request.recv(4096)
            heads.append(buf)
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            data = self.request.recv(4096)
            self.request.sendall(data.upper())

    srv = _Upstream(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_forwarder_adds_credentials_and_relays_bytes():
    heads = []
    up = _fake_proxy(heads)
    pool = TunnelPool()
    try:
        url = f"http://alice:s3cret@127.0.0.1:{up.server_address[1]}"
        local = pool.chrome_proxy_server(url)
        assert local.startswith("http://127.0.0.1:") and "s3cret" not in local
        assert pool.chrome_proxy_server(url) == local  # one listener per proxy
        c = socket.create_connection(("127.0.0.1", int(local.rsplit(":", 1)[1])), timeout=5)
        c.sendall(b"CONNECT muse.ai:443 HTTP/1.1\r\nHost: muse.ai:443\r\nProxy-Authorization: Basic stale\r\n\r\n")
        reply = b""
        while b"\r\n\r\n" not in reply:
            reply += c.recv(4096)
        assert reply.startswith(b"HTTP/1.1 200")
        c.sendall(b"ping")
        assert c.recv(4096) == b"PING"
        c.close()
        token = base64.b64encode(b"alice:s3cret").decode().encode()
        assert heads[0].count(b"Proxy-Authorization") == 1 and token in heads[0]
    finally:
        pool.close_all()
        up.shutdown()
        up.server_close()


def test_proxy_without_password_is_used_directly():
    pool = TunnelPool()
    assert pool.chrome_proxy_server("socks5://9.9.9.9:1080") == "socks5://9.9.9.9:1080"
    assert pool.chrome_proxy_server("http://9.9.9.9:3128") == "http://9.9.9.9:3128"


def test_renew_uses_proxy_and_failure_is_a_proxy_error_without_the_url():
    seen = {}

    def ok(*a, **k):
        seen.update(k)
        return SimpleNamespace(status_code=200, json=lambda: {"status": "assigned", "vm_state": "RUNNING"},
                               cookies=requests.cookies.RequestsCookieJar())

    with patch("requests.get", side_effect=ok), patch("requests.post", side_effect=ok):
        r = engine_module.MuseEngine.renew_session_http({"hatch_sess": "x"}, wake_vm=False, proxy_url="http://u:pw@h:80")
    assert r["ok"] and seen["proxies"]["https"] == "http://u:pw@h:80"

    boom = requests.exceptions.ProxyError("Cannot connect to proxy u:pw@h")
    with patch("requests.get", side_effect=boom):
        with pytest.raises(MuseProxyError) as e:
            engine_module.MuseEngine.renew_session_http({"hatch_sess": "x"}, wake_vm=False, proxy_url="http://u:pw@h:80")
    assert "pw" not in str(e.value) and str(e.value).startswith(proxy_support.PROXY_ERROR)

    # the same network error without a proxy keeps its old meaning
    with patch("requests.get", side_effect=requests.ConnectionError("x")):
        with pytest.raises(engine_module.MuseGenerationError) as e:
            engine_module.MuseEngine.renew_session_http({"hatch_sess": "x"}, wake_vm=False)
    assert not isinstance(e.value, MuseProxyError)


def make_engine(tmp):
    cfg = SimpleNamespace(profile_dir=str(tmp / "profile"), data_dir=str(tmp / "data"), home_dir=str(tmp),
                          extra_path="", chromium="chrome", cdp_port=1, download_dir=str(tmp), login_wait=1)
    return engine_module.MuseEngine(cfg)


class FakeBrowser:
    def __init__(self, fail_context=False):
        self.calls = []
        self.fail_context = fail_context

    def send(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "Target.createBrowserContext":
            if self.fail_context:
                raise RuntimeError("no context")
            return {"result": {"browserContextId": "ctx1"}}
        if method == "Target.createTarget":
            return {"result": {"targetId": "t1"}}
        return {}


def test_context_gets_the_account_proxy(tmp_path):
    eng = make_engine(tmp_path)
    eng.browser = FakeBrowser()
    with patch.object(engine_module, "CDP", lambda *a, **k: SimpleNamespace(send=lambda *a, **k: {})):
        eng._open_page("socks5://9.9.9.9:1080")
    method, params = eng.browser.calls[0]
    assert method == "Target.createBrowserContext"
    assert params == {"proxyServer": "socks5://9.9.9.9:1080", "proxyBypassList": ""}


def test_proxied_account_never_falls_back_to_a_plain_tab(tmp_path):
    eng = make_engine(tmp_path)
    eng.browser = FakeBrowser(fail_context=True)
    with patch("requests.put") as put, pytest.raises(MuseProxyError):
        eng._open_page("http://9.9.9.9:3128")
    put.assert_not_called()
    eng.browser = None
    with patch("requests.put") as put, pytest.raises(MuseProxyError):
        eng._open_page("http://9.9.9.9:3128")
    put.assert_not_called()


def test_reused_page_is_dropped_when_the_proxy_changes(tmp_path):
    eng = make_engine(tmp_path)
    proxies = {"a1": "http://1.1.1.1:80"}
    eng.proxy_resolver = proxies.get
    page = SimpleNamespace(js=lambda *_: True, close=lambda: None, send=lambda *a, **k: {})
    eng.page, eng.current_acc_id, eng.current_proxy = page, "a1", "http://1.1.1.1:80"
    with patch.object(eng, "_wait_ws_ready", return_value=True):
        assert eng.ensure_page({}, None, account_id="a1") is page
        proxies["a1"] = "http://2.2.2.2:80"
        opened = []
        with patch.object(eng, "renew_session_http", return_value={}), \
                patch.object(eng, "_open_page", side_effect=lambda p: opened.append(p) or page), \
                patch.object(eng, "_apply_cookies"):
            eng.ensure_page({}, None, account_id="a1")
    assert opened == ["http://2.2.2.2:80"]


def test_public_account_hides_the_proxy_url():
    acc = {"id": "a", "cookies": {"x": "1"}, "proxy_url": "http://u:pw@h.example:8080"}
    pub = Store.public_account(acc)
    assert "proxy_url" not in pub and "cookies" not in pub and "pw" not in str(pub)
    assert pub["has_proxy"] is True and pub["proxy_host"] == "h.example:8080"
    assert Store.public_account({"id": "b", "cookies": {}})["has_proxy"] is False


def test_a_dead_proxy_is_reported_by_the_admin_endpoints_not_a_500(tmp_path, monkeypatch):
    """Keepalive and quota catch MuseGenerationError; a proxy failure must be one."""
    assert issubclass(MuseProxyError, engine_module.MuseGenerationError)
    assert not issubclass(MuseProxyError, engine_module.MuseAuthError)


def _connect_proxy(status_line, seen):
    class H(socketserver.BaseRequestHandler):
        def handle(self):
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                buf += chunk
            seen.append(buf)
            self.request.sendall(status_line + b"\r\n\r\n")

    srv = _Upstream(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_probe_asks_only_the_proxy_for_a_tunnel_to_a_neutral_address():
    seen = []
    ok = _connect_proxy(b"HTTP/1.1 200 Connection established", seen)
    try:
        assert probe_proxy(f"http://alice:s3cret@127.0.0.1:{ok.server_address[1]}", timeout=3) is None
        head = seen[0].decode()
        assert head.startswith("CONNECT 1.1.1.1:443 HTTP/1.1")
        assert "muse" not in head.lower()
        assert "Proxy-Authorization: Basic " + base64.b64encode(b"alice:s3cret").decode() in head
    finally:
        ok.shutdown()
        ok.server_close()


def test_probe_reports_a_refused_login_a_closed_port_and_a_proxy_that_cannot_reach_out():
    for line in (b"HTTP/1.1 407 Proxy Authentication Required", b"HTTP/1.1 502 Bad Gateway"):
        bad = _connect_proxy(line, [])
        try:
            assert probe_proxy(f"http://u:p@127.0.0.1:{bad.server_address[1]}", timeout=3) == "proxy_refused"
        finally:
            bad.shutdown()
            bad.server_close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert probe_proxy(f"http://127.0.0.1:{port}", timeout=2) == "network"
    assert probe_proxy(f"socks5://127.0.0.1:{port}", timeout=2) in ("proxy_refused", "network")


def test_probe_times_out_on_a_proxy_that_never_answers():
    silent = _Upstream(("127.0.0.1", 0), type("H", (socketserver.BaseRequestHandler,), {"handle": lambda self: self.request.recv(4096) and __import__("time").sleep(2)}))
    threading.Thread(target=silent.serve_forever, daemon=True).start()
    try:
        assert probe_proxy(f"http://127.0.0.1:{silent.server_address[1]}", timeout=0.5) == "timeout"
    finally:
        silent.shutdown()
        silent.server_close()
