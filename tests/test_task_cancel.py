"""Offline checks for task cancellation: engine stop click, cancel endpoint, queued cancel."""
import asyncio
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as engine_module  # noqa: E402


async def call(app, method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else b""
    headers = {"content-type": "application/json", "content-length": str(len(body))}
    if app_module().CFG.api_key:
        headers["authorization"] = "Bearer " + app_module().CFG.api_key
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
    raw = b"".join(m.get("body", b"") for m in messages)
    return status, (json.loads(raw) if raw else {})


def app_module():
    import app
    return app


def request(method, path, payload=None):
    return asyncio.run(call(app_module().app, method, path, payload))


def wait_status(task_id, wanted, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        task = app_module().store.get_task(task_id)
        if task and task.get("status") == wanted:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} never reached {wanted}: {app_module().store.get_task(task_id)}")


def test_engine_clicks_stop_and_raises_cancelled():
    clock = [0.0]
    eng = object.__new__(engine_module.MuseEngine)
    eng._scroll_bottom = lambda: None
    clicked = []

    def js(expr, await_promise=False, timeout=None):
        if expr == eng._STOP_JS:
            clicked.append(clock[0])
            return "clicked"
        if expr == eng._SESSION_STATUS_JS:
            return "running"
        return json.dumps({"cnt": 0, "txt": "", "stop": True, "tail": ""})

    eng.page = SimpleNamespace(js=js)
    eng.attachments = lambda: []
    stop = threading.Event()
    fake_time = SimpleNamespace(time=lambda: clock[0],
                                sleep=lambda n: (clock.__setitem__(0, clock[0] + n),
                                                 clock[0] >= 5 and stop.set()))
    with patch.object(engine_module, "time", fake_time):
        try:
            eng._wait_attachment("", 600, "video", stop_event=stop)
        except engine_module.MuseSessionStopped as exc:
            assert exc.status == "cancelled"
        else:
            raise AssertionError("cancelled wait did not raise")
    assert len(clicked) == 1 and clicked[0] < 7, clicked


def patched_app(generate):
    app = app_module()
    accounts = [{"id": "acc-1", "label": "A1", "enabled": True, "cookies": {"c": "1"}}]
    return accounts, (
        patch("store._write", lambda *a, **k: None),
        patch.object(app.store, "mark", lambda *a, **k: None),
        patch.object(app.store, "accounts", accounts),
        patch.object(app.engine, "start", return_value=None),
        patch.object(app.engine, "generate", side_effect=generate),
        patch.object(app.engine, "reset_thread", return_value=None),
        patch.object(app.engine, "close_current_page", return_value=None),
        patch.object(app, "_renew_and_persist", return_value=None),
        patch.object(app, "_sync_cookies", return_value=None),
    )


def run_with(patches, fn):
    for p in patches:
        p.start()
    try:
        return fn()
    finally:
        for p in reversed(patches):
            p.stop()


def test_cancel_running_video_task():
    app = app_module()
    started, calls = threading.Event(), []

    def fake_generate(cookies, prompt, expect, timeout, expires=None, account_id=None,
                      on_progress=None, reference_image=None, stop_event=None, on_session=None):
        calls.append(account_id)
        started.set()
        while not stop_event.is_set():
            time.sleep(0.01)
        raise engine_module.MuseSessionStopped("cancelled")

    _, patches = patched_app(fake_generate)

    def scenario():
        status, body = request("POST", "/v1/videos", {"prompt": "p", "duration": 10})
        assert status == 200, body
        tid = body["id"]
        assert started.wait(5)
        status, body = request("POST", f"/v1/tasks/{tid}/cancel")
        assert status == 200 and body["status"] == "cancelling", body
        task = wait_status(tid, "cancelled")
        assert task["error"] == "任务已取消"
        assert tid not in app.TASK_CANCEL
        status, _ = request("POST", f"/v1/tasks/{tid}/cancel")
        assert status == 409
        status, _ = request("POST", "/v1/tasks/does-not-exist/cancel")
        assert status == 404

    run_with(patches, scenario)
    assert calls == ["acc-1"], calls
    assert len(app.ACCOUNT_POOL.busy_accounts) == 0


def test_cancel_queued_image_task_never_reaches_muse():
    app = app_module()
    calls = []

    def fake_generate(*a, **k):
        calls.append(1)
        return {"filename": "x.png", "size": 1, "kind": "image"}

    _, patches = patched_app(fake_generate)

    def scenario():
        # Fill every browser slot so the new task stays queued
        held = [app.GEN_LOCK.acquire(timeout=1) for _ in range(app.GEN_LOCK.limit)]
        assert all(held)
        try:
            status, body = request("POST", "/v1/images/tasks", {"prompt": "p"})
            assert status == 202, body
            tid = body["id"]
            status, _ = request("POST", f"/v1/tasks/{tid}/cancel")
            assert status == 200
            wait_status(tid, "cancelled")
        finally:
            for _ in held:
                app.GEN_LOCK.release()

    run_with(patches, scenario)
    assert calls == []


def test_unified_task_lookup():
    app = app_module()
    with patch("store._write", lambda *a, **k: None):
        img = app.store.create_task("image", "p")
        app.store.update_task(img["id"], status="completed", progress=100, response_format="url",
                              result={"filename": "a.png", "size": 1, "kind": "image",
                                      "url": "http://x/v1/media/a.png"})
        vid = app.store.create_task("video", "p")
        app.store.update_task(vid["id"], status="cancelled", error="任务已取消")

    status, body = request("GET", f"/v1/tasks/{img['id']}")
    assert status == 200 and body["kind"] == "image" and body["status"] == "completed"
    assert body["data"][0]["url"].endswith("/v1/media/a.png"), body
    status, body = request("GET", f"/v1/tasks/{vid['id']}")
    assert status == 200 and body["kind"] == "video" and body["status"] == "cancelled"
    status, _ = request("GET", "/v1/tasks/does-not-exist")
    assert status == 404


def test_cancel_while_waiting_for_busy_account():
    app = app_module()
    calls = []

    def fake_generate(*a, **k):
        calls.append(1)
        return {"filename": "x.png", "size": 1, "kind": "image"}

    _, patches = patched_app(fake_generate)

    def scenario():
        # Browser slots are free but the only account is at its per-account limit
        held = [app.ACCOUNT_POOL.acquire(account_id="acc-1", timeout=1)
                for _ in range(app.ACCOUNT_POOL.per_account)]
        try:
            status, body = request("POST", "/v1/images/tasks", {"prompt": "p"})
            assert status == 202, body
            tid = body["id"]
            time.sleep(0.2)
            t0 = time.monotonic()
            status, _ = request("POST", f"/v1/tasks/{tid}/cancel")
            assert status == 200
            wait_status(tid, "cancelled", timeout=3)
            assert time.monotonic() - t0 < 1.5
        finally:
            for acc in held:
                app.ACCOUNT_POOL.release(acc["id"])

    run_with(patches, scenario)
    assert calls == []
    assert len(app.ACCOUNT_POOL.busy_accounts) == 0
