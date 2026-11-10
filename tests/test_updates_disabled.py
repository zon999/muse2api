"""The Studio build never pulls or pushes the upstream repo: the update routes are off and make no GitHub call."""
import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def app_module():
    with tempfile.TemporaryDirectory() as home:
        os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home, MUSE2API_KEY="test-only")
        spec = importlib.util.spec_from_file_location("muse_updates_disabled", ROOT / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        yield module


def test_update_check_reports_disabled_without_network(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_check_update_sync", lambda *a, **k: pytest.fail("GitHub was queried"))
    status = asyncio.run(app_module.admin_check_update(force=True))
    assert status["disabled"] is True and status["has_update"] is False


@pytest.mark.parametrize("route", ["admin_upgrade_now", "admin_repo_push"])
def test_upgrade_and_push_are_refused(app_module, monkeypatch, route):
    monkeypatch.setattr(app_module, "_upgrade_from_github_sync", lambda: pytest.fail("upstream was pulled"))
    with pytest.raises(app_module.HTTPException) as err:
        asyncio.run(getattr(app_module, route)({}))
    assert err.value.status_code == 403
