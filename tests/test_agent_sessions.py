"""Offline regressions for Muse agent jobs driven by Muse Studio: stopping a detached thread, long [raw] prompts."""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("muse_engine_agent_sessions", ROOT / "engine.py")
engine_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine_module)


def stopping_engine(statuses, cancelled=True):
    """An engine whose page opens any thread and whose gateway reports `statuses` in turn."""
    clock = SimpleNamespace(now=0.0)
    engine_module.time = SimpleNamespace(time=lambda: clock.now,
                                         sleep=lambda n: setattr(clock, "now", clock.now + n))
    engine = engine_module.MuseEngine(SimpleNamespace(profile_dir="/tmp", chat_timeout=60))
    opened, clicks = [], []
    engine._open_thread = lambda cookies, sid, expires, account_id: opened.append((sid, account_id))
    seq = iter(statuses)
    last = [statuses[-1] if statuses else ""]
    engine._muse_session_status = lambda: next(seq, last[0])
    engine._gateway_cancel = lambda sid: clicks.append(sid) or cancelled
    return engine, opened, clicks


def test_stop_cancels_and_waits_for_interrupted():
    engine, opened, clicks = stopping_engine(["running", "running", "running", "interrupted"])
    assert engine.stop_session({}, "thread-abc123", account_id="acc1") == ("interrupted", True)
    assert opened == [("thread-abc123", "acc1")] and len(clicks) == 1


def test_stop_on_finished_thread_does_nothing():
    engine, _, clicks = stopping_engine(["interrupted"])
    assert engine.stop_session({}, "thread-abc123") == ("interrupted", False)
    assert clicks == []


def test_stop_without_running_agent_reports_not_stopped():
    engine, _, clicks = stopping_engine(["idle"], cancelled=False)
    assert engine.stop_session({}, "thread-abc123") == ("idle", False)
    assert len(clicks) == 1


def test_stop_unknown_thread_raises_not_found():
    engine, _, clicks = stopping_engine([""])
    with pytest.raises(engine_module.MuseSessionNotFound):
        engine.stop_session({}, "thread-abc123")
    assert clicks == []


def test_long_raw_video_prompt_is_sent_whole():
    with tempfile.TemporaryDirectory() as home:
        os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home, MUSE2API_KEY="test-only")
        app_spec = importlib.util.spec_from_file_location("muse_agent_sessions_app", ROOT / "app.py")
        app = importlib.util.module_from_spec(app_spec)
        sys.modules[app_spec.name] = app
        app_spec.loader.exec_module(app)
        body = "".join(f"line {i:05d} keeps $HOME and `ticks` literal\n" for i in range(1500))
        assert len(body) > 60_000
        prompt = app.build_video_prompt(app.VideoRequest(prompt="[raw] " + body, images=["i"], size="9:16"))
        assert prompt == body.strip()
