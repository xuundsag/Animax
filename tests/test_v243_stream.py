from __future__ import annotations

import asyncio
import time

import pytest

from animax.job import Job
from animax.metadata import select_best_anime
from animax.settings import Settings
from animax.streaming import (
    SKIPPED,
    PreparedEpisode,
    StreamManager,
    StreamRequestError,
    StreamSession,
    parse_stream_request,
)


def test_stream_request_accepts_mal_id_and_yes_skip():
    request = parse_stream_request("21 1 100 Yes")
    assert request.query == "21"
    assert (request.start, request.end) == (1, 100)
    assert request.skip_intro_outro is True
    assert request.language == "sub"


def test_stream_request_fuzzy_title_options_are_order_independent():
    request = parse_stream_request('"One Piece" 4 12 dub No')
    assert request.query == "One Piece"
    assert request.skip_intro_outro is False
    assert request.language == "dub"


def test_stream_request_defaults_to_no_skip_and_rejects_bad_range():
    request = parse_stream_request("Attack on Titan 1 3")
    assert request.skip_intro_outro is False
    with pytest.raises(StreamRequestError, match="100 episodes"):
        parse_stream_request("One Piece 1 101")


def test_title_search_selects_the_highest_fuzzy_match():
    mal_id, title, score = select_best_anime(
        "One Pice",
        [
            {"idMal": 1535, "title": {"english": "Death Note", "romaji": "Death Note"}},
            {"idMal": 21, "title": {"english": "One Piece", "romaji": "Wan Pisu"}},
        ],
    )
    assert (mal_id, title) == (21, "One Piece")
    assert score > 70


@pytest.mark.asyncio
async def test_stream_status_shows_download_style_episode_progress(tmp_path):
    bridge = FakeBridge()
    manager = StreamManager(Settings(), bridge, tmp_path)
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
    session.playback_duration = 1440
    session.playback_state = "Starting"
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
        expected = "**One Piece** | `E1` | Streaming | 0:10 / 24:00 (0%)"
        assert manager.status(1)["message"].startswith(expected)
        await manager._update_progress_message(session)
        assert bridge.edits[-1][2].startswith(expected)
    finally:
        session.task.cancel()
        await asyncio.gather(session.task, return_exceptions=True)


class FakeBridge:
    def __init__(self):
        self.manager = None
        self.added = []
        self.started = []
        self.completed_at = {}
        self.prefetch_events = {}
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


@pytest.mark.asyncio
async def test_pipeline_prefetches_only_the_next_episode_during_playback(
    tmp_path, monkeypatch
):
    import animax.streaming as streaming

    async def search(query, _overrides):
        return 21, "One Piece", 99

    monkeypatch.setattr(streaming, "resolve_anime", search)
    settings = Settings(keep_stream_cache=True)
    bridge = FakeBridge()
    manager = StreamManager(settings, bridge, tmp_path)
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
        return PreparedEpisode(episode, work, work / "episode.mp4", 0.08)

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
        raw="One Piece 1 3 Yes",
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
        message.startswith("**One Piece** | `E1` |") for _, message in bridge.messages
    )
    assert any(
        message.startswith("**One Piece** | `E3` | Finished | ")
        for _, _, message in bridge.edits
    )


@pytest.mark.asyncio
async def test_skip_cancels_the_prefetched_episode_at_a_boundary(tmp_path):
    bridge = FakeBridge()
    manager = StreamManager(Settings(), bridge, tmp_path)
    request = parse_stream_request("21 1 3")
    from animax.streaming import StreamSession

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
    from animax.streaming import StreamSession

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
    from animax.streaming import StreamSession

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
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from test_debug_release import build

    client, ctx, *_ = build(tmp_path, monkeypatch)
    channel = SimpleNamespace(id=200)
    member = SimpleNamespace(voice=SimpleNamespace(channel=channel))
    ctx.guild = SimpleNamespace(id=100, get_member=lambda _user_id: member)
    ctx.author = SimpleNamespace(id=123)
    ctx.send = AsyncMock()
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


@pytest.mark.asyncio
async def test_cancelled_episode_preparation_removes_partial_cache(tmp_path):
    bridge = FakeBridge()
    manager = StreamManager(Settings(keep_stream_cache=False), bridge, tmp_path)
    request = parse_stream_request("21 1 2")
    from animax.streaming import StreamSession

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
