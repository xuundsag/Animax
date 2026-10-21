from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from animax.settings import FIELDS, Settings
from animax.stream_api import create_api
from animax.stream_runtime import (
    _sidecar_environment,
    _validate_stream_health,
    _version_tuple,
)
from animax.streaming import StreamManager, StreamSession, parse_stream_request


class NullBridge:
    async def send(self, *_args):
        return {"ok": True}

    async def series_seek(self, _series_id, seconds, *, absolute=False):
        return {
            "position": seconds,
            "message": "Seeked to 6:07." if absolute else "Seek applied.",
        }


def test_node_runtime_version_parser():
    assert _version_tuple("v22.4.0") == (22, 4, 0)
    assert _version_tuple("v24.1.2") > (22, 4, 0)
    assert _version_tuple("v20.20.2") < (22, 4, 0)
    assert _version_tuple("unknown") is None


def test_stream_health_requires_ready_distinct_user_id():
    _validate_stream_health(
        {"discordReady": True, "userId": "stream-user"}, "main-user"
    )
    with pytest.raises(RuntimeError, match="not ready"):
        _validate_stream_health(
            {"discordReady": False, "userId": "stream-user"}, "main-user"
        )
    with pytest.raises(RuntimeError, match="user ID"):
        _validate_stream_health({"discordReady": True}, "main-user")
    with pytest.raises(RuntimeError, match="different streaming user"):
        _validate_stream_health(
            {"discordReady": True, "userId": "main-user"}, "main-user"
        )


def test_sidecar_receives_only_the_separate_streaming_token(tmp_path):
    environment = _sidecar_environment(
        "stream-token",
        tmp_path,
        "local-secret",
        12345,
        {"DISCORD_TOKEN": "main-token", "OTHER": "kept"},
    )
    assert "DISCORD_TOKEN" not in environment
    assert environment["STREAM_DISCORD_TOKEN"] == "stream-token"
    assert environment["OTHER"] == "kept"


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
    sidecar = (
        Path(__file__).resolve().parents[1] / "animax" / "stream_bridge.mjs"
    ).read_text()
    assert "process.env.STREAM_DISCORD_TOKEN" in sidecar
    assert "client.login(streamToken)" in sidecar
    assert "process.env.DISCORD_TOKEN" not in sidecar


@pytest.mark.asyncio
async def test_internal_progress_route_updates_current_episode(tmp_path):
    secret = "local-test-secret"
    manager = StreamManager(Settings(), NullBridge(), tmp_path)
    session = StreamSession(
        "series",
        1,
        2,
        3,
        4,
        parse_stream_request("One Piece 1 2"),
        title="One Piece",
        current_episode=1,
    )
    session.task = asyncio.create_task(asyncio.sleep(30))
    manager.session = session
    client = TestClient(TestServer(create_api(manager, secret)))
    await client.start_server()
    try:
        response = await client.post(
            "/internal/progress",
            json={
                "seriesId": "series",
                "episode": 1,
                "kind": "episode",
                "elapsed": 10,
                "duration": 1440,
            },
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert response.status == 200
        assert session.playback_state == "Streaming"
        assert session.playback_duration == 1440
        assert session.playback_elapsed == 10
    finally:
        session.task.cancel()
        await asyncio.gather(session.task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_voice_member_can_absolute_seek_via_local_control_api(tmp_path):
    secret = "local-test-secret"
    manager = StreamManager(Settings(), NullBridge(), tmp_path)
    session = StreamSession(
        "series",
        1,
        2,
        3,
        4,
        parse_stream_request("One Piece 1 2"),
        current_episode=1,
        series_started=True,
        playback_state="Streaming",
        playback_duration=2400,
    )
    session.task = asyncio.create_task(asyncio.sleep(30))
    manager.session = session
    client = TestClient(TestServer(create_api(manager, secret)))
    await client.start_server()
    headers = {"Authorization": f"Bearer {secret}"}
    try:
        response = await client.post(
            "/stream/control",
            json={
                "guildId": 1,
                "voiceChannelId": 2,
                "authorId": 99,
                "command": "seek",
                "value": 367,
            },
            headers=headers,
        )
        assert response.status == 200
        assert (await response.json())["message"] == "Seeked to 6:07."
        assert session.playback_elapsed == 367

        denied = await client.post(
            "/stream/control",
            json={
                "guildId": 1,
                "voiceChannelId": 9,
                "authorId": 99,
                "command": "seek",
                "value": 367,
            },
            headers=headers,
        )
        assert denied.status == 400
        assert "voice channel" in (await denied.json())["error"]
    finally:
        session.task.cancel()
        await asyncio.gather(session.task, return_exceptions=True)
        await client.close()


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
