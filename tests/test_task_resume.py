"""Offline checks for resuming image/video tasks after a restart via the recorded Muse thread."""
import asyncio
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as engine_module  # noqa: E402

SID = "thread-abc123"


def no_wait():
    clock = [0.0]
    return SimpleNamespace(time=lambda: clock[0], sleep=lambda n: clock.__setitem__(0, clock[0] + n))


def test_wait_loop_reports_thread_id_once():
    eng = object.__new__(engine_module.MuseEngine)
    eng._scroll_bottom = lambda: None
    ticks = [0]

    def js(expr, await_promise=False, timeout=None):
        if expr == eng._THREAD_ID_JS:
            ticks[0] += 1
            return "" if ticks[0] < 3 else SID
        return json.dumps({"cnt": 0, "txt": "", "stop": True, "tail": ""})

    eng.page = SimpleNamespace(js=js)
    eng.attachments = lambda: [{"src": "out", "tid": "image", "w": 1, "h": 1}] if ticks[0] >= 5 else []
    reported = []
    with patch.object(engine_module, "time", no_wait()):
        eng._wait_attachment("", 60, "image", on_session=reported.append)
    assert reported == [SID]


def test_resume_opens_the_recorded_thread():
    eng = object.__new__(engine_module.MuseEngine)
    navigated = []
    eng.ensure_page = lambda *a, **k: None
    eng._wait_ws_ready = lambda *a, **k: True
    eng.page = SimpleNamespace(
        send=lambda method, params: navigated.append(params["url"]),
        js=lambda expr, **k: SID if expr == eng._THREAD_ID_JS else True)
    eng._wait_attachment = lambda *a, **k: {"src": "out"}
    eng._save_attachment = lambda att, expect: {"filename": "a.mp4", "size": 3, "kind": expect}
    with patch.object(engine_module, "time", no_wait()):
        res = eng.resume({}, SID, expect="video", timeout=30)
    assert navigated == [f"https://muse.ai/thread/{SID}"]
    assert res["kind"] == "video"

    navigated.clear()
    try:
        eng.resume({}, "../evil", expect="video", timeout=30)
    except engine_module.MuseGenerationError:
        pass
    else:
        raise AssertionError("invalid thread id accepted")
    assert navigated == []


def app_patches(app, accounts, **engine_methods):
    patches = [
        patch("store._write", lambda *a, **k: None),
        patch.object(app.store, "mark", lambda *a, **k: None),
        patch.object(app.store, "accounts", accounts),
        patch.object(app.engine, "start", return_value=None),
        patch.object(app.engine, "reset_thread", return_value=None),
        patch.object(app.engine, "close_current_page", return_value=None),
        patch.object(app, "_renew_and_persist", return_value=None),
        patch.object(app, "_sync_cookies", return_value=None),
    ]
    patches += [patch.object(app.engine, name, side_effect=fn) for name, fn in engine_methods.items()]
    return patches


def run_with(patches, fn):
    for p in patches:
        p.start()
    try:
        return fn()
    finally:
        for p in reversed(patches):
            p.stop()


def wait_status(app, tid, wanted, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        task = app.store.get_task(tid)
        if task and task.get("status") == wanted:
            return task
        time.sleep(0.02)
    raise AssertionError(f"{tid} never reached {wanted}: {app.store.get_task(tid)}")


def test_running_task_records_thread_and_account():
    import app
    accounts = [{"id": "acc-1", "label": "A1", "enabled": True, "cookies": {"c": "1"}}]

    def fake_generate(cookies, prompt, expect, timeout, on_session=None, **kwargs):
        on_session(SID)
        task = next(t for t in app.store.tasks.values() if t.get("prompt") == "record-me")
        assert task["muse_session"] == SID and task["account"] == "acc-1"
        return {"filename": "a.mp4", "size": 3, "kind": "video"}

    def scenario():
        req = app.VideoRequest(prompt="record-me")
        body = asyncio.run(app.create_video(req))
        task = wait_status(app, body["id"], "completed")
        assert task["muse_session"] == SID

    run_with(app_patches(app, accounts, generate=fake_generate), scenario)


def test_startup_resumes_tasks_with_a_thread_and_fails_the_rest():
    import app
    accounts = [{"id": "acc-1", "label": "A1", "enabled": True, "cookies": {"c": "1"}}]
    resumed = []

    def fake_resume(cookies, session_id, expect="image", **kwargs):
        resumed.append((session_id, expect, kwargs.get("account_id")))
        return {"filename": "r.png", "size": 1, "kind": "image"}

    async def no_keepalive():
        return None

    def scenario():
        with patch("store._write", lambda *a, **k: None):
            ok = app.store.create_task("image", "p")
            app.store.update_task(ok["id"], status="processing", account="acc-1", muse_session=SID)
            lost = app.store.create_task("video", "p")
            app.store.update_task(lost["id"], status="processing")
            gone = app.store.create_task("image", "p")
            app.store.update_task(gone["id"], status="processing", account="deleted", muse_session=SID)
        with patch.object(app.CFG, "api_key", "test-key"), \
             patch.object(app, "_keepalive_loop", no_keepalive):
            asyncio.run(app._startup())
        task = wait_status(app, ok["id"], "completed")
        assert task["result"]["filename"] == "r.png"
        assert app.store.get_task(lost["id"])["status"] == "failed"
        assert app.store.get_task(gone["id"])["status"] == "failed"
        assert resumed == [(SID, "image", "acc-1")]
        assert len(app.ACCOUNT_POOL.busy_accounts) == 0
        assert ok["id"] not in app.TASK_CANCEL

    run_with(app_patches(app, accounts, resume=fake_resume), scenario)
