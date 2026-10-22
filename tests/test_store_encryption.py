"""Offline checks for optional accounts.json encryption (MUSE2API_DATA_KEY)."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store as store_module  # noqa: E402

COOKIES = {"hatch_sess": "secret-sess", "hatch_native_auth_device": "secret-dev"}


def make_cfg(tmp, key=""):
    data = Path(tmp)
    return SimpleNamespace(accounts_file=str(data / "accounts.json"),
                           tasks_file=str(data / "tasks.json"), data_key=key)


def test_plaintext_without_key_is_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_cfg(tmp)
        store_module.Store(cfg).add_account(COOKIES, "a")
        assert json.loads(Path(cfg.accounts_file).read_text())[0]["cookies"] == COOKIES


def test_encrypted_round_trip_hides_cookie_values():
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_cfg(tmp, key)
        store_module.Store(cfg).add_account(COOKIES, "a")
        raw = Path(cfg.accounts_file).read_text()
        assert raw.startswith(store_module._ENC_PREFIX)
        assert "secret-sess" not in raw and "hatch_sess" not in raw
        assert store_module.Store(cfg).accounts[0]["cookies"] == COOKIES


def test_plaintext_file_is_migrated_when_key_is_set():
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as tmp:
        store_module.Store(make_cfg(tmp)).add_account(COOKIES, "a")
        cfg = make_cfg(tmp, key)
        s = store_module.Store(cfg)
        assert s.accounts[0]["cookies"] == COOKIES
        assert Path(cfg.accounts_file).read_text().startswith(store_module._ENC_PREFIX)


def test_encrypted_file_without_or_with_wrong_key_refuses_to_start():
    key = Fernet.generate_key().decode()
    with tempfile.TemporaryDirectory() as tmp:
        store_module.Store(make_cfg(tmp, key)).add_account(COOKIES, "a")
        before = Path(make_cfg(tmp).accounts_file).read_text()
        for bad in ("", Fernet.generate_key().decode()):
            try:
                store_module.Store(make_cfg(tmp, bad))
            except RuntimeError as exc:
                assert "MUSE2API_DATA_KEY" in str(exc)
            else:
                raise AssertionError("store loaded an encrypted file without the right key")
        assert Path(make_cfg(tmp).accounts_file).read_text() == before
