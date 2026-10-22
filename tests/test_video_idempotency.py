"""Offline check: a resent video request with the same Idempotency-Key never starts a second generation."""
import asyncio
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_task_resume import app_patches, run_with, wait_status  # noqa: E402


def test_same_key_returns_the_existing_video_task():
    import app
    accounts = [{"id": "acc-1", "label": "A1", "enabled": True, "cookies": {"c": "1"}}]
    calls = []

    def fake_generate(cookies, prompt, expect, timeout, **kwargs):
        calls.append(prompt)
        return {"filename": "v.mp4", "size": 3, "kind": "video"}

    def scenario():
        req = app.VideoRequest(prompt="idempotent video", duration=10)
        first = asyncio.run(app.create_video(req, idempotency_key="studio-task:0"))
        wait_status(app, first["id"], "completed")
        again = asyncio.run(app.create_video(req, idempotency_key="studio-task:0"))
        assert again["id"] == first["id"]
        assert len(calls) == 1
        with pytest.raises(HTTPException) as clash:
            asyncio.run(app.create_video(app.VideoRequest(prompt="other"), idempotency_key="studio-task:0"))
        assert clash.value.status_code == 409
        plain = asyncio.run(app.create_video(req))
        assert plain["id"] != first["id"]
        wait_status(app, plain["id"], "completed")

    run_with(app_patches(app, accounts, generate=fake_generate), scenario)
