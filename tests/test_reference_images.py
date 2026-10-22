"""Offline checks for multiple reference images: API collection/limits and per-file upload confirmation."""
import asyncio
import base64
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as engine_module  # noqa: E402


def data_url(n_bytes, fill=b"x"):
    return "data:image/png;base64," + base64.b64encode(fill * n_bytes).decode()


def fake_engine(confirm=True):
    """Engine whose composer confirms each injected file by adding one attachment."""
    eng = object.__new__(engine_module.MuseEngine)
    eng._clear_attachments = lambda: None
    state = {"attached": 0, "names": []}

    def js(expr, await_promise=False, timeout=None):
        if expr == eng._ATTACHED_COUNT_JS:
            return state["attached"]
        if "new DataTransfer()" in expr:
            state["names"].append(json.loads(expr.rsplit(",", 1)[1].split(")")[0]))
            if confirm:
                state["attached"] += 1
            return json.dumps({"ok": True})
        return None

    eng.page = SimpleNamespace(js=js)
    return eng, state


def no_wait():
    clock = [0.0]
    return SimpleNamespace(time=lambda: clock[0], sleep=lambda n: clock.__setitem__(0, clock[0] + n))


def test_engine_attaches_each_reference_and_waits_for_confirmation():
    eng, state = fake_engine()
    with patch.object(engine_module, "time", no_wait()):
        eng._attach_image([data_url(10, b"a"), data_url(10, b"b"), data_url(10, b"c")])
    assert state["names"] == ["reference_image_1", "reference_image_2", "reference_image_3"]
    assert state["attached"] == 3


def test_engine_single_reference_keeps_original_name():
    eng, state = fake_engine()
    with patch.object(engine_module, "time", no_wait()):
        eng._attach_image(data_url(10))
    assert state["names"] == ["reference_image"]


def test_engine_rejects_unconfirmed_upload_and_oversized_total():
    eng, _ = fake_engine(confirm=False)
    with patch.object(engine_module, "time", no_wait()):
        try:
            eng._attach_image([data_url(10)])
        except engine_module.MuseGenerationError as exc:
            assert "未确认" in str(exc)
        else:
            raise AssertionError("unconfirmed upload accepted")

    eng, state = fake_engine()
    big = [data_url(3 * 1024 * 1024, fill) for fill in (b"a", b"b", b"c")]
    with patch.object(engine_module, "time", no_wait()):
        try:
            eng._attach_image(big)
        except engine_module.MuseGenerationError as exc:
            assert "8MB" in str(exc)
        else:
            raise AssertionError("oversized references accepted")
    assert state["names"] == [], "nothing may be uploaded when the total is too large"


async def call(app, method, path, payload):
    body = json.dumps(payload).encode()
    headers = {"content-type": "application/json", "content-length": str(len(body))}
    import app as app_mod
    if app_mod.CFG.api_key:
        headers["authorization"] = "Bearer " + app_mod.CFG.api_key
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        "server": ("test", 80), "client": ("test", 1),
    }
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


def test_api_collects_all_reference_fields_and_enforces_limits():
    import app

    seen, done = [], threading.Event()

    def fake_run(prompt, kind, timeout, reference_image=None, **kwargs):
        seen.append((kind, reference_image))
        done.set()
        return {"filename": "x.png", "size": 1, "kind": kind}, "acc-1"

    with patch.object(app, "_run_generation", side_effect=fake_run), \
         patch("store._write", lambda *a, **k: None):
        status, body = asyncio.run(call(app.app, "POST", "/v1/images/tasks", {
            "prompt": "p",
            "reference_images": ["https://a/1.png", {"url": "https://a/2.png"}],
            "images": [{"image_url": {"url": "https://a/3.png"}}, "https://a/1.png"],
        }))
        assert status == 202, body
        assert done.wait(5)
        assert seen[-1] == ("image", ["https://a/1.png", "https://a/2.png", "https://a/3.png"])

        done.clear()
        status, body = asyncio.run(call(app.app, "POST", "/v1/videos", {
            "prompt": "p", "reference_images": ["https://a/1.png", "https://a/2.png"]}))
        assert status == 200, body
        assert done.wait(5)
        assert seen[-1] == ("video", ["https://a/1.png", "https://a/2.png"])

        status, _ = asyncio.run(call(app.app, "POST", "/v1/images/tasks", {
            "prompt": "p", "reference_images": [f"https://a/{i}.png" for i in range(7)]}))
        assert status == 400
        status, _ = asyncio.run(call(app.app, "POST", "/v1/videos", {
            "prompt": "p", "reference_images": [data_url(5 * 1024 * 1024, b"a"),
                                                data_url(5 * 1024 * 1024, b"b")]}))
        assert status == 400
