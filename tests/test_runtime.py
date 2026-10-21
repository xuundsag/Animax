from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from animax.settings import FIELDS, Settings
from animax.stream_api import create_api
from animax.stream_runtime import _version_tuple
from animax.streaming import StreamManager


class NullBridge:
    async def send(self, *_args):
        return {"ok": True}


def test_node_runtime_version_parser():
    assert _version_tuple("v22.4.0") == (22, 4, 0)
    assert _version_tuple("v24.1.2") > (22, 4, 0)
    assert _version_tuple("v20.20.2") < (22, 4, 0)
    assert _version_tuple("unknown") is None


def test_legacy_worker_settings_are_ignored_but_download_settings_remain(tmp_path):
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
    settings = Settings.from_env(path)
    assert settings.quality == 720
    assert settings.caption_font == "dejavu-serif"
    assert settings.upload_mode == "auto"
    assert not hasattr(settings, "max_workers")
    assert "workers" not in FIELDS
    assert settings.keep_stream_cache is False
    assert settings.stream_bitrate_kbps == 5000


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
async def test_local_stream_api_requires_secret_and_has_public_health(tmp_path):
    secret = "local-test-secret"
    manager = StreamManager(Settings(), NullBridge(), tmp_path)
    client = TestClient(TestServer(create_api(manager, secret)))
    await client.start_server()
    try:
        denied = await client.post("/stream/status", json={})
        assert denied.status == 401
        health = await client.get("/health")
        assert health.status == 200
        assert (await health.json())["service"] == "animax-stream-coordinator"
        authorized = await client.post(
            "/internal/series-event",
            json={"seriesId": "unknown"},
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert authorized.status == 200
    finally:
        await client.close()
