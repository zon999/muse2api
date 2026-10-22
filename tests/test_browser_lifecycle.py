"""Offline checks: concurrent start() launches one Chromium, and stop() closes a reattached one."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine as engine_module  # noqa: E402


class FakeProc:
    def poll(self):
        return None


def make_engine(tmp):
    cfg = SimpleNamespace(profile_dir=str(tmp / "profile"), data_dir=str(tmp / "data"), home_dir=str(tmp),
                          extra_path="", chromium="chrome", cdp_port=1)
    return engine_module.MuseEngine(cfg)


def test_concurrent_start_launches_one_browser(tmp_path):
    eng = make_engine(tmp_path)
    launches = []
    state = {"up": False}

    def fake_popen(*a, **k):
        launches.append(1)
        threading.Event().wait(0.2)  # real delay; time.sleep is patched below
        state["up"] = True
        return FakeProc()

    def fake_http_json(url, timeout=1):
        if not state["up"]:
            raise OSError("not up")
        return {"webSocketDebuggerUrl": "ws://x"}

    with patch.object(engine_module.subprocess, "Popen", side_effect=fake_popen), \
         patch.object(engine_module, "http_json", side_effect=fake_http_json), \
         patch.object(engine_module, "CDP", side_effect=lambda *a, **k: SimpleNamespace()), \
         patch.object(engine_module.time, "sleep", lambda n: None):
        threads = [threading.Thread(target=eng.start) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(launches) == 1, launches


def test_stop_closes_reattached_browser(tmp_path):
    eng = make_engine(tmp_path)
    sent = []
    eng.browser = SimpleNamespace(send=lambda method, *a, **k: sent.append(method), close=lambda: None)
    eng.proc = None
    eng.stop()
    assert sent == ["Browser.close"]
