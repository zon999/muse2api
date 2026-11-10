"""Offline regressions for chat streaming: a re-rendered bubble must never repeat the reply."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("muse_engine_chat_stream", ROOT / "engine.py")
engine_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine_module)

ZW = "​"


def run_chat(polls):
    """Feeds (cnt, text, stop) page states to chat_stream and returns the joined reply."""
    clock = [0.0]
    engine_module.time = SimpleNamespace(time=lambda: clock[0], sleep=lambda n: clock.__setitem__(0, clock[0] + n))
    engine = engine_module.MuseEngine(SimpleNamespace(profile_dir="/tmp", chat_timeout=60))
    engine.ensure_page = lambda *a, **k: None
    engine.reset_thread = lambda **k: None
    engine._send = lambda prompt: None
    states = iter([(0, "", False)] + polls)
    last = [polls[-1]]

    def poll():
        state = next(states, last[0])
        return state

    engine._poll_chat = poll
    return "".join(engine.chat_stream({}, "hi"))


def test_list_marker_rerender_is_not_repeated():
    head = "Fruits\nApple: crisp\nMango: sweet\n"
    reply = run_chat([
        (1, "Fruits", True),
        (1, head + "-" + ZW, True),
        (1, head + "Grape: small", True),
        (1, head + "Grape: small\nRed", False),
    ])
    assert reply == head + "Grape: small\nRed"


def test_numbered_marker_held_until_rendered():
    reply = run_chat([
        (1, "Colors\n1.", True),
        (1, "Colors\nRed\nBlue", False),
    ])
    assert reply == "Colors\nRed\nBlue"


def test_new_bubble_keeps_both_bubbles():
    reply = run_chat([
        (1, "Checking files", True),
        (1, "Checking files\n-" + ZW, True),
        (2, "Done: 3 files", True),
        (2, "Done: 3 files found", False),
    ])
    assert reply == "Checking files\n\nDone: 3 files found"


def test_plain_streaming_is_unchanged():
    reply = run_chat([
        (1, "Hello", True),
        (1, "Hello wor", True),
        (1, "Hello world.", False),
    ])
    assert reply == "Hello world."


def test_working_agent_is_not_cut_at_first_token_timeout():
    # 45s of tool work (stop button shown, no text) before the first words: past the old 40s first-token limit.
    working = [(0, "", True)] * 750
    reply = run_chat(working + [(1, "Saved compiled.md", True), (1, "Saved compiled.md", False)])
    assert reply == "Saved compiled.md"


def test_settled_text_rules():
    settle = engine_module.MuseEngine._settled_chat_text
    assert settle("a\n-" + ZW) == "a\n"
    assert settle("a\n## ") == "a\n"
    assert settle("a\n3)") == "a\n"
    assert settle("a\nb") == "a\nb"
    assert settle("-" + ZW) == ""
    assert settle("a\n- item") == "a\n- item"
