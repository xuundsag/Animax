from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from animax.bootstrap import _version_tuple
from animax.bot import create_api
from animax.settings import Settings
from animax.streaming import StreamManager


class NullBridge:
    async def send(self, *_args):
        return {"ok": True}


def test_node_runtime_version_parser():
    assert _version_tuple("v22.4.0") == (22, 4, 0)
    assert _version_tuple("v24.1.2") > (22, 4, 0)
    assert _version_tuple("v20.20.2") < (22, 4, 0)
    assert _version_tuple("unknown") is None


def test_old_worker_settings_are_ignored_and_stream_is_fixed_1080p(
    tmp_path, monkeypatch
):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "workers": 6,
                "max_workers": 4,
                "upload_mode": "auto",
                "quality": 720,
                "caption_font": "dejavu-serif",
            }
        )
    )
    monkeypatch.delenv("CAPTION_FONT", raising=False)
    settings = Settings.from_env(path)
    assert settings.quality == 1080
    assert settings.caption_font == "dejavu-serif"
    assert not hasattr(settings, "max_workers")
    assert not hasattr(settings, "upload_mode")


def test_package_pins_current_dave_compatible_sidecar():
    root = Path(__file__).resolve().parents[1]
    package = json.loads((root / "package.json").read_text())
    assert package["engines"]["node"] == ">=22.4.0"
    assert package["dependencies"]["@dank074/discord-video-stream"] == "7.0.0"
    assert package["dependencies"]["@lng2004/discord.js-selfbot-v13"] == "3.7.2"
    lock = json.loads((root / "package-lock.json").read_text())
    locked = lock["packages"]["node_modules/@dank074/discord-video-stream"]
    assert locked["version"] == "7.0.0"


@pytest.mark.asyncio
async def test_local_bridge_requires_secret_and_returns_worker_free_settings(tmp_path):
    secret = "local-test-secret"
    manager = StreamManager(Settings(), NullBridge(), tmp_path)
    client = TestClient(TestServer(create_api(manager, secret)))
    await client.start_server()
    try:
        denied = await client.post("/settings/get", json={})
        assert denied.status == 401
        health = await client.get("/health")
        assert health.status == 200
        authorized = await client.post(
            "/settings/get",
            json={},
            headers={"Authorization": f"Bearer {secret}"},
        )
        payload = await authorized.json()
        assert authorized.status == 200
        assert "workers: removed" in payload["message"]
        assert "1080p" in payload["message"]
    finally:
        await client.close()
