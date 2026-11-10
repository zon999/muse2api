"""Offline regressions for a message left in the composer: click send once, then give up after 60s."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("muse_engine_composer_resend", ROOT / "engine.py")
engine_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine_module)


def run_wait(sent_by_click: bool, timeout: int = 120):
    """Waits for a video while the composer still holds one attachment; returns (outcome, send clicks)."""
    clock = SimpleNamespace(now=0.0)
    engine_module.time = SimpleNamespace(time=lambda: clock.now,
                                         sleep=lambda n: setattr(clock, "now", clock.now + n))
    engine = engine_module.MuseEngine(SimpleNamespace(profile_dir="/tmp", chat_timeout=60))
    page = SimpleNamespace(unsent=1, clicks=0, sent_at=None)

    def js(script, **_kw):
        if script == engine._ATTACHED_COUNT_JS:
            return page.unsent
        if "textarea" in script:
            return 0
        if "b.click();return 'clicked'" in script:
            page.clicks += 1
            if sent_by_click:
                page.unsent, page.sent_at = 0, clock.now
            return "clicked"
        # After the send click the agent works (Stop shown) until the video is attached.
        return json.dumps({"cnt": 0, "txt": "", "stop": page.sent_at is not None, "tail": "",
                           "path": "/thread/abc123"})

    engine.page = SimpleNamespace(js=js)
    engine._scroll_bottom = lambda: None
    engine._muse_session_status = lambda: "running"

    def attachments():
        if page.sent_at is not None and clock.now > page.sent_at + 20:
            return [{"tid": "hatch-chat-attachment-presentation-video", "src": "blob:v", "vSrc": "blob:v",
                     "hasVideo": True, "w": 720, "h": 1280}]
        return []

    engine.attachments = attachments
    try:
        att = engine._wait_attachment("", timeout, "video")
        return ("video" if att and att.get("vSrc") else "none"), page.clicks
    except engine_module.MuseGenerationError as exc:
        return str(exc), page.clicks


def test_stuck_message_is_sent_again_once():
    outcome, clicks = run_wait(sent_by_click=True)
    assert outcome == "video"
    assert clicks == 1


def test_message_still_stuck_after_a_minute_fails():
    outcome, clicks = run_wait(sent_by_click=False)
    assert "没有发出去" in outcome
    assert clicks == 1
