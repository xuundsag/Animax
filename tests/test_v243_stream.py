from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from animax.bot import MegaPlayBot
from animax.job import Job
from animax.settings import Settings
from animax.streaming import (
    SKIPPED,
    PreparedEpisode,
    StreamManager,
    StreamRequestError,
    StreamSession,
    parse_stream_request,
    resolve_tracked_anime,
    select_uploaded_episode,
)


def sample_library(*, languages=("dub", "sub"), episode=1):
    data = {"version": 1, "anime": {"21": {"title": "One Piece", "episodes": {}}}}
    for language in languages:
        data["anime"]["21"]["episodes"].setdefault(language, {})[str(episode)] = {
            "deliveries": {
                "111:222": {
                    "account_id": "111",
                    "channel_id": "222",
                    "status": "uploaded",
                    "messages": [
                        {
                            "message_id": f"{episode}001",
                            "uploaded_at": "2026-01-01T00:00:00+00:00",
                            "guild_id": "333",
                            "size_bytes": 1234,
                            "jump_url": "https://discord.com/channels/333/222/1001",
                        }
                    ],
                }
            }
        }
    return SimpleNamespace(data=data)


class FakeUI:
    def __init__(self):
        self.events = []

    def event(self, message, style="cyan"):
        self.events.append((message, style))


class FakeStatusMessage:
    def __init__(self, message_id="main-status"):
        self.id = message_id
        self.contents = []

    async def edit(self, *, content):
        self.contents.append(content)
        return self


class FakeBridge:
    def __init__(self):
        self.manager = None
        self.added = []
        self.started = []
        self.completed_at = {}
        self.skip_calls = []
        self.messages = []
        self.edits = []
        self._completion_tasks = set()

    async def send(self, channel_id, content):
        self.messages.append((channel_id, content))
        return {"ok": True, "messageId": f"progress-{channel_id}"}

    async def edit(self, channel_id, message_id, content):
        self.edits.append((channel_id, message_id, content))
        return {"ok": True, "messageId": message_id}

    async def join(self, guild_id, voice_id):
        return {"ok": True}

    async def series_start(self, *args, **kwargs):
        self.started.append((args, kwargs))
        return {"ok": True}

    async def series_add(self, series_id, episode, kind, file_path, duration):
        self.added.append((episode, kind, file_path))
        if kind == "episode":

            async def complete_later():
                await asyncio.sleep(0.08)
                self.completed_at[episode] = time.monotonic()
                await self.manager.handle_segment_event(
                    {"seriesId": series_id, "episode": episode, "kind": "episode"}
                )

            task = asyncio.create_task(complete_later())
            self._completion_tasks.add(task)
            task.add_done_callback(self._completion_tasks.discard)
        return {"ok": True}

    async def series_skip(self, series_id, episode):
        self.skip_calls.append(episode)
        return {"ok": True}

    async def series_end(self, series_id):
        await self.manager.handle_series_event({"seriesId": series_id})
        return {"ok": True}

    async def series_abort(self, series_id):
        for task in list(self._completion_tasks):
            task.cancel()
        return {"ok": True}

    async def leave(self, guild_id):
        return {"ok": True}


def test_stream_request_supports_optional_language_and_no_skip_argument():
    request = parse_stream_request("21 1 100 dub")
    assert request.query == "21"
    assert (request.start, request.end) == (1, 100)
    assert request.language == "dub"

    request = parse_stream_request('"One Piece" 4 12')
    assert request.query == "One Piece"
    assert request.language is None
    with pytest.raises(StreamRequestError, match="sub.*dub"):
        parse_stream_request("21 1 12 Yes")
    with pytest.raises(StreamRequestError, match="100 episodes"):
        parse_stream_request("One Piece 1 101")


def test_stream_title_lookup_is_fuzzy_but_strictly_local_to_anime_json():
    library = sample_library()
    assert resolve_tracked_anime("21", library.data) == (21, "One Piece", 100)
    mal_id, title, score = resolve_tracked_anime("One Pice", library.data)
    assert (mal_id, title) == (21, "One Piece")
    assert score > 70
    with pytest.raises(Exception, match="not tracked in anime.json"):
        resolve_tracked_anime("1535", library.data)


def test_upload_selection_uses_requested_language_and_prefers_current_guild():
    data = sample_library(languages=("dub",)).data
    deliveries = data["anime"]["21"]["episodes"]["dub"]["1"]["deliveries"]
    deliveries["999:888"] = {
        "account_id": "999",
        "channel_id": "888",
        "status": "uploaded",
        "messages": [
            {
                "message_id": "9001",
                "uploaded_at": "2026-10-01T00:00:00+00:00",
                "guild_id": "444",
            }
        ],
    }
    selected = select_uploaded_episode(
        data, 21, 1, "dub", preferred_guild_id=444, preferred_account_id=111
    )
    assert selected["message_id"] == "9001"
    assert selected["channel_id"] == "888"
    assert selected["language"] == "dub"
    with pytest.raises(Exception, match="No uploaded sub attachment"):
        select_uploaded_episode(data, 21, 1, "sub")


def test_auto_language_prefers_dub_then_sub():
    data = sample_library(languages=("sub", "dub")).data
    assert select_uploaded_episode(data, 21, 1)["language"] == "dub"
    data["anime"]["21"]["episodes"]["dub"]["1"]["deliveries"].clear()
    assert select_uploaded_episode(data, 21, 1)["language"] == "sub"


@pytest.mark.asyncio
async def test_main_command_account_fetches_video_attachment_from_saved_message():
    attachment = SimpleNamespace(
        filename="episode.mkv",
        content_type="video/x-matroska",
        size=2048,
        url="https://cdn.discordapp.com/attachments/222/1001/episode.mkv?ex=123",
    )
    message = SimpleNamespace(
        id=1001, attachments=[SimpleNamespace(filename="cover.jpg"), attachment]
    )
    channel = SimpleNamespace(id=222, fetch_message=AsyncMock(return_value=message))
    fake_bot = SimpleNamespace(
        channel_for=AsyncMock(return_value=channel),
        ui=FakeUI(),
    )
    result = await MegaPlayBot.stream_attachment_for_record(
        fake_bot,
        {"channel_id": "222", "message_id": "1001"},
    )
    assert result["url"] == attachment.url
    assert result["filename"] == "episode.mkv"
    assert result["message_id"] == "1001"
    channel.fetch_message.assert_awaited_once_with(1001)


@pytest.mark.asyncio
async def test_prepare_episode_downloads_and_validates_recorded_discord_attachment(
    tmp_path, monkeypatch
):
    import animax.streaming as streaming

    library = sample_library(languages=("dub",)).data
    ui = FakeUI()
    bridge = FakeBridge()
    lookup_records = []

    async def lookup(record):
        lookup_records.append(record)
        return {
            "url": "https://cdn.discordapp.com/attachments/222/1001/episode.mp4?ex=123",
            "filename": "episode.mp4",
            "content_type": "video/mp4",
            "size": 2048,
            "message_id": record["message_id"],
        }

    async def download(url, destination, **kwargs):
        assert "cdn.discordapp.com" in url
        destination.write_bytes(b"fake video attachment")
        kwargs["on_progress"](1024, 2048)
        kwargs["on_progress"](2048, 2048)
        return 2048

    async def validate(path, **kwargs):
        assert path.read_bytes() == b"fake video attachment"
        return path, {
            "format": {"duration": "120"},
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
        }

    monkeypatch.setattr(streaming, "download_discord_attachment", download)
    monkeypatch.setattr(streaming, "ensure_playable", validate)
    manager = StreamManager(
        Settings(keep_stream_cache=True),
        bridge,
        tmp_path,
        library=SimpleNamespace(data=library),
        attachment_lookup=lookup,
        ui=ui,
        command_account_id="111",
    )
    session = StreamSession(
        "series",
        333,
        555,
        666,
        777,
        parse_stream_request("21 1 1 dub"),
        mal_id=21,
        title="One Piece",
        playback_episode=1,
    )

    prepared = await manager._prepare_episode(session, 1)

    assert prepared.episode == 1
    assert prepared.duration == 120
    assert prepared.language == "dub"
    assert prepared.message_id == "1001"
    assert prepared.path.exists()
    assert lookup_records[0]["channel_id"] == "222"
    assert lookup_records[0]["message_id"] == "1001"
    marker = json.loads((prepared.work / "ready.json").read_text())
    assert marker["message_id"] == "1001"
    assert marker["channel_id"] == "222"
    assert marker["language"] == "dub"
    assert any(
        "DOWNLOAD_PROGRESS" in message and "50%" in message for message, _ in ui.events
    )
    assert any("EPISODE_READY" in message for message, _ in ui.events)


@pytest.mark.asyncio
async def test_attachment_downloader_streams_to_disk_and_rejects_untrusted_urls(
    tmp_path, monkeypatch
):
    import animax.stream_media as stream_media

    class FakeContent:
        async def iter_chunked(self, _size):
            yield b"video-"
            yield b"bytes"

    class FakeResponse:
        status = 200
        headers = {"Content-Length": "11"}
        content = FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, _url):
            return FakeResponse()

    monkeypatch.setattr(stream_media.aiohttp, "ClientSession", FakeClient)
    destination = tmp_path / "downloads" / "stream-cache" / "episode.mp4"
    updates = []
    amount = await stream_media.download_discord_attachment(
        "https://cdn.discordapp.com/attachments/222/1001/episode.mp4?ex=123",
        destination,
        max_bytes=1024,
        timeout=20,
        expected_size=11,
        disk_root=tmp_path / "downloads",
        on_progress=lambda received, total: updates.append((received, total)),
    )
    assert amount == 11
    assert destination.read_bytes() == b"video-bytes"
    assert updates[-1] == (11, 11)
    with pytest.raises(stream_media.MediaError, match="trusted Discord CDN"):
        await stream_media.download_discord_attachment(
            "https://example.com/video.mp4",
            destination,
            max_bytes=1024,
            timeout=20,
        )


@pytest.mark.asyncio
async def test_status_message_is_created_by_main_client_and_keeps_progress_style(
    tmp_path,
):
    bridge = FakeBridge()
    status_message = FakeStatusMessage()
    manager = StreamManager(Settings(), bridge, tmp_path)
    session = StreamSession(
        "series",
        1,
        2,
        3,
        4,
        parse_stream_request("One Piece 1 2 dub"),
        title="One Piece",
        current_episode=1,
        progress_message=status_message,
    )
    session.task = asyncio.create_task(asyncio.sleep(30))
    session.playback_duration = 1440
    session.playback_state = "Starting"
    session.selected_languages[1] = "dub"
    manager.session = session
    try:
        await manager._update_progress_message(session)
        await manager.handle_progress_event(
            {
                "seriesId": "series",
                "episode": 1,
                "kind": "episode",
                "elapsed": 10,
                "duration": 1440,
            }
        )
        expected = "**One Piece** | `E1/2` | Streaming (DUB) | 0:10 / 24:00 (0%)"
        assert manager.status(1)["message"].startswith(expected)
        await manager._update_progress_message(session)
        assert status_message.contents[-1].startswith(expected)
        assert bridge.messages == []
        assert bridge.edits == []
    finally:
        session.task.cancel()
        await asyncio.gather(session.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pipeline_prefetches_only_the_next_uploaded_episode_during_playback(
    tmp_path, monkeypatch
):
    settings = Settings(keep_stream_cache=True)
    bridge = FakeBridge()
    manager = StreamManager(
        settings,
        bridge,
        tmp_path,
        library=sample_library(languages=("dub",)),
    )
    bridge.manager = manager
    join_finished_at = {}

    async def delayed_join(_guild_id, _voice_id):
        await asyncio.sleep(0.06)
        join_finished_at["at"] = time.monotonic()
        return {"ok": True}

    monkeypatch.setattr(bridge, "join", delayed_join)
    starts = {}
    active_preparations = 0
    peak_preparations = 0

    async def prepare(session, episode):
        nonlocal active_preparations, peak_preparations
        active_preparations += 1
        peak_preparations = max(peak_preparations, active_preparations)
        starts[episode] = time.monotonic()
        await asyncio.sleep(0.02)
        active_preparations -= 1
        work = tmp_path / "downloads" / f"fake-{episode}"
        return PreparedEpisode(
            episode, work, work / "episode.mp4", 0.08, language="dub"
        )

    async def make_card(session, episode):
        folder = session.card_directory
        folder.mkdir(parents=True, exist_ok=True)
        card = folder / f"card-{episode}.mp4"
        card.touch()
        return card

    monkeypatch.setattr(manager, "_prepare_episode", prepare)
    monkeypatch.setattr(manager, "_make_card", make_card)
    await manager.start(
        guild_id=100,
        voice_channel_id=200,
        text_channel_id=300,
        author_id=400,
        raw="One Piece 1 3 dub",
    )
    session = manager.session
    assert session is not None
    await asyncio.wait_for(session.task, timeout=5)

    assert [ep for ep, kind, _ in bridge.added if kind == "episode"] == [1, 2, 3]
    assert starts[1] < join_finished_at["at"]
    assert starts[2] < bridge.completed_at[1]
    assert starts[3] > bridge.completed_at[1]
    assert starts[3] < bridge.completed_at[2]
    assert peak_preparations == 1
    assert bridge.started
    assert bridge.started[0][1]["preset"] == "ultrafast"
    assert any(
        message.startswith("**One Piece** | `E1/3` |") for _, message in bridge.messages
    )
    assert any(
        message.startswith("**One Piece** | `E3/3` | Finished | ")
        for _, _, message in bridge.edits
    )


@pytest.mark.asyncio
async def test_stream_failure_updates_same_progress_message_and_is_logged(
    tmp_path, monkeypatch
):
    bridge = FakeBridge()
    ui = FakeUI()
    status_message = FakeStatusMessage()
    manager = StreamManager(
        Settings(), bridge, tmp_path, library=sample_library(languages=("dub",)), ui=ui
    )

    async def failed_prepare(_session, episode):
        raise RuntimeError(f"upload for E{episode} is missing")

    monkeypatch.setattr(manager, "_prepare_episode", failed_prepare)
    await manager.start(
        guild_id=100,
        voice_channel_id=200,
        text_channel_id=300,
        author_id=400,
        raw="21 1 1 dub",
        progress_message=status_message,
    )
    await asyncio.wait_for(manager.session.task, timeout=3)

    assert status_message.contents
    assert "Failed" in status_message.contents[-1]
    assert "upload for E1 is missing" in status_message.contents[-1]
    assert any("FAILED" in message for message, _ in ui.events)
    assert bridge.messages == []


@pytest.mark.asyncio
async def test_skip_cancels_the_prefetched_episode_at_a_boundary(tmp_path):
    bridge = FakeBridge()
    manager = StreamManager(Settings(), bridge, tmp_path)
    request = parse_stream_request("21 1 3")
    session = StreamSession("session", 1, 2, 3, 4, request, prefetch_episode=2)
    session.task = asyncio.create_task(asyncio.sleep(30))
    session.prefetch_task = asyncio.create_task(asyncio.sleep(30))
    manager.session = session
    try:
        result = await manager.skip(guild_id=1, author_id=4)
        assert "E2" in result["message"]
        await asyncio.sleep(0)
        assert session.prefetch_task.cancelled()
        assert 2 in session.skip_preparing
    finally:
        session.task.cancel()
        await asyncio.gather(
            session.task, session.prefetch_task, return_exceptions=True
        )


@pytest.mark.asyncio
async def test_skip_control_is_limited_to_original_command_author(tmp_path):
    bridge = FakeBridge()
    manager = StreamManager(Settings(), bridge, tmp_path)
    request = parse_stream_request("21 1 2")
    session = StreamSession(
        "session", 1, 2, 3, 4, request, series_started=True, current_episode=1
    )
    session.task = asyncio.create_task(asyncio.sleep(30))
    manager.session = session
    try:
        with pytest.raises(StreamRequestError, match="Only the command author"):
            await manager.skip(guild_id=1, author_id=99)
        result = await manager.skip(guild_id=1, author_id=4)
        assert result["ok"] is True
        assert bridge.skip_calls == [1]
    finally:
        session.task.cancel()
        await asyncio.gather(session.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_is_limited_to_original_author_and_signals_session(tmp_path):
    bridge = FakeBridge()
    manager = StreamManager(Settings(), bridge, tmp_path)
    request = parse_stream_request("21 1 2")
    session = StreamSession("session", 1, 2, 3, 4, request)
    session.task = asyncio.create_task(asyncio.sleep(30))
    manager.session = session
    try:
        with pytest.raises(StreamRequestError, match="Only the command author"):
            await manager.stop(guild_id=1, author_id=99)
        result = await manager.stop(guild_id=1, author_id=4)
        assert result["ok"] is True
        assert session.stop_event.is_set()
    finally:
        session.task.cancel()
        await asyncio.gather(session.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stream_start_reservation_blocks_download_launch(tmp_path, monkeypatch):
    from test_debug_release import build

    client, ctx, *_ = build(tmp_path, monkeypatch)
    channel = SimpleNamespace(id=200)
    member = SimpleNamespace(voice=SimpleNamespace(channel=channel))
    ctx.guild = SimpleNamespace(id=100, get_member=lambda _user_id: member)
    ctx.author = SimpleNamespace(id=123)
    status_message = FakeStatusMessage()
    ctx.send = AsyncMock(return_value=status_message)
    manager = SimpleNamespace(
        session=None,
        start=AsyncMock(return_value={"message": "Stream starting."}),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def ensure_runtime():
        client.stream_manager = manager
        entered.set()
        await release.wait()
        return manager

    client.ensure_stream_runtime = ensure_runtime
    command = client.get_command("stream")
    starting = asyncio.create_task(command.callback(ctx, request="21 1 1"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert client.stream_starting
    accepted = await client.launch_job(
        ctx, ctx.channel, Job(21, "dub", 1, 1, 456), False, fixed_target=ctx.channel
    )
    assert accepted is False
    assert "Go Live stream is active" in ctx.send.await_args.args[0]
    release.set()
    await asyncio.wait_for(starting, timeout=2)
    assert not client.stream_starting
    manager.start.assert_awaited_once()
    assert manager.start.await_args.kwargs["progress_message"] is status_message


@pytest.mark.asyncio
async def test_cancelled_episode_preparation_removes_partial_cache(tmp_path):
    bridge = FakeBridge()
    manager = StreamManager(Settings(keep_stream_cache=False), bridge, tmp_path)
    request = parse_stream_request("21 1 2")
    session = StreamSession("session", 1, 2, 3, 4, request, mal_id=21)
    session.prefetch_episode = 2
    session.skip_preparing.add(2)
    session.prefetch_task = asyncio.create_task(asyncio.Event().wait())
    work = manager._episode_work(session, 2)
    work.mkdir(parents=True)
    (work / "partial.mp4").write_bytes(b"partial")
    session.prefetch_task.cancel()

    result = await manager._await_prepare(session, 2, session.prefetch_task)

    assert result is SKIPPED
    assert not work.exists()
