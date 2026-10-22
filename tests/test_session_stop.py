"""Offline checks: the wait loop ends early when the Muse session stops, and no retry follows an interruption."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as engine_module  # noqa: E402


def make_engine(session_status, attachments_at=None):
    """Engine whose page reports a running generation and the given Muse session status."""
    clock = [0.0]
    eng = object.__new__(engine_module.MuseEngine)
    eng._scroll_bottom = lambda: None
    calls = []

    def js(expr, await_promise=False, timeout=None):
        if expr == eng._SESSION_STATUS_JS:
            calls.append(clock[0])
            return session_status(clock[0])
        return json.dumps({"cnt": 0, "txt": "", "stop": True, "tail": ""})

    eng.page = SimpleNamespace(js=js)
    eng.attachments = lambda: ([{"src": "generated", "tid": "image", "w": 1, "h": 1}]
                               if attachments_at is not None and clock[0] >= attachments_at else [])
    fake_time = SimpleNamespace(time=lambda: clock[0], sleep=lambda n: clock.__setitem__(0, clock[0] + n))
    return eng, clock, calls, fake_time


def test_interrupted_session_ends_wait_early():
    eng, clock, calls, fake_time = make_engine(lambda t: "running" if t < 20 else "interrupted")
    with patch.object(engine_module, "time", fake_time):
        try:
            eng._wait_attachment("", 600, "video")
        except engine_module.MuseSessionStopped as exc:
            assert exc.status == "interrupted"
        else:
            raise AssertionError("wait loop ignored the interrupted session")
    assert clock[0] < 40, clock[0]
    # status is polled every ~8s, not on every 0.6s tick
    assert len(calls) <= 5, calls


def test_unknown_status_keeps_waiting_for_result():
    eng, clock, calls, fake_time = make_engine(lambda t: "", attachments_at=50)
    with patch.object(engine_module, "time", fake_time):
        att = eng._wait_attachment("", 600, "image")
    assert att["src"] == "generated"
    assert calls, "session status was never checked"


def test_interrupted_generation_is_not_retried_on_another_account():
    import app

    accounts = [
        {"id": "acc-1", "label": "A1", "enabled": True, "cookies": {"c": "1"}},
        {"id": "acc-2", "label": "A2", "enabled": True, "cookies": {"c": "2"}},
    ]
    used = []

    def fake_generate(cookies, prompt, expect, timeout, expires=None, account_id=None,
                      on_progress=None, reference_image=None, stop_event=None):
        used.append(account_id)
        raise engine_module.MuseSessionStopped("interrupted")

    orig = list(app.store.accounts)
    try:
        with patch("store._write", lambda *a, **k: None), \
             patch.object(app.store, "mark", lambda *a, **k: None), \
             patch.object(app.engine, "start", return_value=None), \
             patch.object(app.engine, "generate", side_effect=fake_generate), \
             patch.object(app.engine, "reset_thread", return_value=None), \
             patch.object(app.engine, "close_current_page", return_value=None), \
             patch.object(app, "_renew_and_persist", return_value=None), \
             patch.object(app, "_sync_cookies", return_value=None):
            app.store.accounts = accounts
            try:
                app._run_generation("p", "image", timeout=5)
            except engine_module.MuseSessionStopped:
                pass
            else:
                raise AssertionError("interrupted generation should fail")
    finally:
        app.store.accounts = orig
    assert len(used) == 1, used
    assert len(app.ACCOUNT_POOL.busy_accounts) == 0
