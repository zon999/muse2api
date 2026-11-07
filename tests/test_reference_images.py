"""Muse Studio sends references as `reference_images` (data URLs); the API must keep accepting that field.

Upload mechanics and upstream's own fields are covered by test_reference_media*.py and test_reference_limits.py.
"""
import asyncio
import base64
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def data_url(fill: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(fill).decode()


async def call(app, method, path, payload):
    body = json.dumps(payload).encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [(b"content-type", b"application/json"), (b"host", b"127.0.0.1")],
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
    messages, sent = [], False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    return status, json.loads(b"".join(m.get("body", b"") for m in messages) or b"{}")


def test_reference_images_field_reaches_generation_in_order():
    import app

    seen, done = [], threading.Event()

    def fake_run(prompt, kind, timeout, reference_images=None, **kwargs):
        seen.append((kind, reference_images))
        done.set()
        return {"filename": "x.png", "size": 1, "kind": kind}, "acc-1"

    a, b, c = data_url(b"a"), data_url(b"b"), data_url(b"c")
    with patch.object(app, "_run_generation", side_effect=fake_run), \
         patch("store._write", lambda *args, **kwargs: None):
        status, body = asyncio.run(call(app.app, "POST", "/v1/images/tasks", {
            "prompt": "p", "images": [a], "reference_images": [b, c]}))
        assert status == 202, body
        assert done.wait(5)
        assert seen[-1] == ("image", [a, b, c])

        done.clear()
        status, body = asyncio.run(call(app.app, "POST", "/v1/videos", {
            "prompt": "p", "reference_images": [b, a]}))
        assert status == 200, body
        assert done.wait(5)
        assert seen[-1] == ("video", [b, a])

        status, _ = asyncio.run(call(app.app, "POST", "/v1/images/tasks", {
            "prompt": "p", "reference_images": [a] * (app.MAX_REFERENCE_IMAGES + 1)}))
        assert status == 400
