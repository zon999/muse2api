"""Account rotation skips accounts whose session failed while a healthy one exists."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MUSE2API_KEY", "test-only")
sys.path.insert(0, str(ROOT))

from config import Config
from store import Store


def _store(home, states):
    cfg = Config(base_dir=home)
    cfg.ensure_dirs()
    store = Store(cfg)
    ids = []
    for i, ok in enumerate(states):
        acc = store.add_account({"hatch_sess": f"fixture{i}"}, f"acc{i}")
        acc["ok"] = ok
        ids.append(acc["id"])
    return store, ids


def test_failed_account_is_not_picked_while_a_healthy_one_is_busy():
    with tempfile.TemporaryDirectory() as home:
        store, (good, bad) = _store(home, [True, False])
        assert store.pick_account(exclude_ids={good}) is None
        assert store.pick_account()["id"] == good


def test_failed_accounts_are_used_when_no_account_is_healthy():
    with tempfile.TemporaryDirectory() as home:
        store, (a, b) = _store(home, [False, False])
        assert store.pick_account()["id"] in (a, b)


def test_disconnected_account_is_switched_off_by_mark():
    with tempfile.TemporaryDirectory() as home:
        store, (acc, other) = _store(home, [True, True])
        store.mark(acc, False, "Muse is disconnected: 账号已被 Muse 断开连接")
        assert store.get_account(acc)["enabled"] is False
        assert store.get_account(other)["enabled"] is True
        assert store.pick_account()["id"] == other


def test_plain_auth_failure_does_not_switch_the_account_off():
    with tempfile.TemporaryDirectory() as home:
        store, (acc,) = _store(home, [True])
        store.mark(acc, False, "会话认证失败")
        assert store.get_account(acc).get("enabled", True) is True


def test_disconnected_screen_text_is_recognised():
    import pytest
    from engine import MuseAccountDisconnected, MuseEngine
    eng = MuseEngine.__new__(MuseEngine)
    with pytest.raises(MuseAccountDisconnected):
        eng._raise_if_disconnected("Muse is disconnected\nWe disconnected Muse to protect your information.")
    eng._raise_if_disconnected("Log in or create an account")


def test_quota_read_on_a_locked_account_raises_disconnected():
    import pytest
    from engine import MuseAccountDisconnected, MuseEngine, MuseGenerationError
    eng = MuseEngine.__new__(MuseEngine)

    class Page:
        def js(self, expr):
            return "Muse is disconnected\nWe disconnected Muse to protect your information."

    eng.page = Page()

    def no_menu(*_a, **_k):
        raise MuseGenerationError("找不到设置入口")

    eng._quota_click = no_menu
    with pytest.raises(MuseAccountDisconnected):
        eng._read_quota_panel()
