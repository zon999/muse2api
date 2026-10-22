"""Offline checks for local-safety defaults: version, bind host, CORS allowlist, media auth."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def call(app, method, path, headers=None):
    """Send one request straight to the ASGI app; returns (status, headers, body)."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "server": ("test", 80), "client": ("test", 1),
    }
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    hdrs = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
    return start["status"], hdrs, b"".join(m.get("body", b"") for m in messages)


def load_app(tmp, **env):
    """Import a fresh app.py with its own Config so env overrides take effect."""
    os.environ["MUSE2API_HOME"] = tmp
    os.environ["MUSE2API_KEY"] = "test-secret"
    for key in ("MUSE2API_HOST", "MUSE2API_CORS_ORIGINS", "MUSE2API_MEDIA_AUTH"):
        os.environ.pop(key, None)
    os.environ.update(env)
    sys.modules.pop("config", None)
    spec = importlib.util.spec_from_file_location("app_hardening", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_version_matches_version_json():
    expected = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))["version"]
    with tempfile.TemporaryDirectory(prefix="muse-hardening-") as tmp:
        module = load_app(tmp)
        assert module.app.version == expected


def test_default_host_is_loopback():
    with tempfile.TemporaryDirectory(prefix="muse-hardening-") as tmp:
        module = load_app(tmp)
        assert module.CFG.host == "127.0.0.1"


def test_cors_only_allows_configured_origins():
    with tempfile.TemporaryDirectory(prefix="muse-hardening-") as tmp:
        module = load_app(tmp)
        preflight = {"access-control-request-method": "POST"}
        _, ok, _ = asyncio.run(call(module.app, "OPTIONS", "/admin/accounts",
                                    {"origin": "https://muse.ai", **preflight}))
        assert ok.get("access-control-allow-origin") == "https://muse.ai"
        _, bad, _ = asyncio.run(call(module.app, "OPTIONS", "/admin/accounts",
                                     {"origin": "https://evil.example", **preflight}))
        assert "access-control-allow-origin" not in bad


def test_cors_wildcard_is_opt_in():
    with tempfile.TemporaryDirectory(prefix="muse-hardening-") as tmp:
        module = load_app(tmp, MUSE2API_CORS_ORIGINS="*")
        _, hdrs, _ = asyncio.run(call(module.app, "GET", "/v1/media/none.webp",
                                      {"origin": "https://any.example"}))
        assert hdrs.get("access-control-allow-origin") == "*"


def test_media_public_by_default_and_protected_when_enabled():
    with tempfile.TemporaryDirectory(prefix="muse-hardening-") as tmp:
        module = load_app(tmp)
        Path(module.CFG.media_dir, "a.webp").write_bytes(b"img")
        status, _, body = asyncio.run(call(module.app, "GET", "/v1/media/a.webp"))
        assert status == 200 and body == b"img"

    with tempfile.TemporaryDirectory(prefix="muse-hardening-") as tmp:
        module = load_app(tmp, MUSE2API_MEDIA_AUTH="1")
        Path(module.CFG.media_dir, "a.webp").write_bytes(b"img")
        status, _, _ = asyncio.run(call(module.app, "GET", "/v1/media/a.webp"))
        assert status == 401
        status, _, body = asyncio.run(call(module.app, "GET", "/v1/media/a.webp",
                                           {"authorization": "Bearer test-secret"}))
        assert status == 200 and body == b"img"


def test_readyz_reports_version():
    with tempfile.TemporaryDirectory(prefix="muse-hardening-") as tmp:
        module = load_app(tmp)
        status, _, body = asyncio.run(call(module.app, "GET", "/readyz"))
        assert status == 200 and json.loads(body)["version"] == module.app.version
