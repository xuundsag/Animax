import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import aiohttp
import pytest
from test_debug_release import build
from test_v24_workers import server

import animax.bot as app
import animax.metadata as metadata
from animax.job import Job
from animax.metadata import TitleError, saved_anime_title
from animax.routing import LogContext
from animax.scheduler import Entry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [TitleError("ClientResponseError, TimeoutError"), asyncio.TimeoutError()]
)
async def test_title_outage_continues_in_manually_selected_channel(
    tmp_path, monkeypatch, error
):
    client, *_ = build(tmp_path, monkeypatch)
    guild, anchor, _, channel = server()
    selected = channel(600, "my-anime-channel")
    lookup = AsyncMock(side_effect=error)
    monkeypatch.setattr(app, "fetch_anime_title", lookup)
    client.run_job = AsyncMock()
    job = Job(999, "dub", 1, 1, selected.id, worker_id=3)
    await client.run_managed_job(
        Entry(LogContext(anchor), anchor, job, False, selected)
    )
    assert job.title == "MAL 999"
    assert client.library.data["routes"]["123:9:77:999"]["channel_id"] == "600"
    assert client.run_job.await_args.args[1] is selected
    assert client.library.data["jobs"][job.id]["title_provisional"] is True
    guild.create_text_channel.assert_not_awaited()
    lookup.assert_awaited_once_with(999)


@pytest.mark.asyncio
async def test_actual_two_provider_failures_reach_safe_title_fallback(
    tmp_path, monkeypatch
):
    client, *_ = build(tmp_path, monkeypatch)

    class Response:
        def __init__(self, exc):
            self.exc = exc

        async def __aenter__(self):
            raise self.exc

        async def __aexit__(self, *args):
            return False

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["timeout"].total == 8

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return Response(
                aiohttp.ClientResponseError(
                    NS(real_url="https://example.invalid"), (), status=503
                )
            )

        def get(self, *args, **kwargs):
            return Response(asyncio.TimeoutError())

    monkeypatch.setattr(metadata.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(app, "fetch_anime_title", metadata.fetch_anime_title)
    assert await client.resolve_job_title(Job(999, "dub", 1, 1, 456)) == (
        "MAL 999",
        "MAL ID fallback",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["anime", "routes", "jobs"])
async def test_cached_real_title_avoids_network(tmp_path, monkeypatch, source):
    client, *_ = build(tmp_path, monkeypatch)
    if source == "anime":
        client.library.record(
            21, "One Piece", "dub", 1, 123, 456, "uploaded", message_id="555"
        )
    else:
        client.library.data.setdefault(source, {})["saved"] = {
            "mal": 21,
            "title": "One Piece",
        }
    lookup = AsyncMock(side_effect=AssertionError("must not use metadata network"))
    monkeypatch.setattr(app, "fetch_anime_title", lookup)
    assert await client.resolve_job_title(Job(21, "dub", 1, 1, 456)) == (
        "One Piece",
        "anime.json",
    )
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_overrides_and_invalid_override_file_do_not_pause(tmp_path, monkeypatch):
    client, *_ = build(tmp_path, monkeypatch)
    path = tmp_path / "title_overrides.json"
    path.write_text(json.dumps({"21": "My Anime Title"}))
    lookup = AsyncMock(return_value="Provider Title")
    monkeypatch.setattr(app, "fetch_anime_title", lookup)
    assert await client.resolve_job_title(Job(21, "dub", 1, 1, 456)) == (
        "My Anime Title",
        "override",
    )
    lookup.assert_not_awaited()
    path.write_text("{malformed")
    assert await client.resolve_job_title(Job(22, "dub", 1, 1, 456)) == (
        "Provider Title",
        "provider",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["One Piece", "MAL 21"])
async def test_resumed_job_keeps_original_caption_identity(
    tmp_path, monkeypatch, title
):
    client, *_ = build(tmp_path, monkeypatch)
    (tmp_path / "title_overrides.json").write_text('{"21": "Changed title"}')
    lookup = AsyncMock(
        side_effect=AssertionError("saved upload identity must not change")
    )
    monkeypatch.setattr(app, "fetch_anime_title", lookup)
    assert await client.resolve_job_title(Job(21, "dub", 1, 1, 456, title=title)) == (
        title,
        "saved job",
    )
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_outage_backoff_shared_by_workers_then_recovers(
    tmp_path, monkeypatch
):
    client, *_ = build(tmp_path, monkeypatch)
    lookup = AsyncMock(side_effect=TitleError("offline"))
    monkeypatch.setattr(app, "fetch_anime_title", lookup)
    results = await asyncio.gather(
        *(client.resolve_job_title(Job(mal, "dub", 1, 1, 456)) for mal in range(1, 7))
    )
    assert lookup.await_count == 1
    assert [row[0] for row in results] == [f"MAL {mal}" for mal in range(1, 7)]
    client.title_retry_after = client.title_next_request = 0
    lookup.side_effect = None
    lookup.return_value = "Recovered Title"
    assert await client.resolve_job_title(Job(7, "dub", 1, 1, 456)) == (
        "Recovered Title",
        "provider",
    )


@pytest.mark.asyncio
async def test_cancellation_during_lookup_does_not_start_or_select_channel(
    tmp_path, monkeypatch
):
    client, *_ = build(tmp_path, monkeypatch)
    guild, anchor, *_ = server()
    started = asyncio.Event()

    async def lookup(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "fetch_anime_title", lookup)
    task = asyncio.create_task(
        client.run_managed_job(
            Entry(LogContext(anchor), anchor, Job(21, "dub", 1, 1, 456), False)
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client.title_lock.locked()
    guild.create_text_channel.assert_not_awaited()
    assert client.title_retry_after == 0


def command_context(guild, anchor):
    return NS(guild=guild, channel=anchor, send=AsyncMock(), message=None)


@pytest.mark.asyncio
async def test_first_use_requires_and_saves_final_channel_mention(
    tmp_path, monkeypatch
):
    client, *_ = build(tmp_path, monkeypatch)
    guild, anchor, _, channel = server()
    selected = channel(600, "manual-target")
    ctx = command_context(guild, anchor)
    client.cfg.delete_trigger = False
    client.channel_for = AsyncMock(return_value=selected)
    queued = []
    client.scheduler.enqueue = lambda entry: queued.append(entry) or True

    await client.download_command(ctx, 21, "dub", 1, 3, "resume", f"<#{selected.id}>")
    assert len(queued) == 1 and queued[0].fixed_target is selected
    assert client.library.data["routes"]["123:9:77:21"]["channel_id"] == "600"
    assert guild.create_text_channel.await_count == 0


@pytest.mark.asyncio
async def test_first_use_without_channel_mention_is_rejected(tmp_path, monkeypatch):
    client, *_ = build(tmp_path, monkeypatch)
    guild, anchor, *_ = server()
    ctx = command_context(guild, anchor)
    client.scheduler.enqueue = lambda entry: pytest.fail(
        "must not queue without a channel"
    )
    await client.download_command(ctx, 21, "dub", 1, 1)
    assert "channel mention at the end" in ctx.send.await_args.args[0]
    assert not client.library.data.get("routes")
    guild.create_text_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeat_download_uses_saved_anime_json_channel_and_ignores_mention(
    tmp_path, monkeypatch
):
    client, *_ = build(tmp_path, monkeypatch)
    guild, anchor, _, channel = server()
    saved = channel(600, "saved-channel")
    other = channel(601, "ignored-channel")
    client.library.data["routes"]["123:9:77:21"] = {
        "account_id": "123",
        "guild_id": "9",
        "category_id": "77",
        "mal": 21,
        "channel_id": "600",
    }
    ctx = command_context(guild, anchor)
    client.cfg.delete_trigger = False
    client.channel_for = AsyncMock(return_value=saved)
    queued = []
    client.scheduler.enqueue = lambda entry: queued.append(entry) or True

    await client.download_command(ctx, 21, "sub", 2, 4, "resume", f"<#{other.id}>")
    assert queued[0].fixed_target is saved
    assert client.channel_for.await_args.args == ("600",)
    assert "channel_id" in client.library.data["routes"]["123:9:77:21"]
    guild.create_text_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_anime_history_also_recovers_saved_upload_channel(
    tmp_path, monkeypatch
):
    client, *_ = build(tmp_path, monkeypatch)
    guild, anchor, _, channel = server()
    saved = channel(600, "old-anime-channel")
    client.library.record(
        21,
        "One Piece",
        "dub",
        1,
        123,
        saved.id,
        "uploaded",
        message_id="900",
        guild_id=guild.id,
    )
    ctx = command_context(guild, anchor)
    client.cfg.delete_trigger = False
    client.channel_for = AsyncMock(return_value=saved)
    queued = []
    client.scheduler.enqueue = lambda entry: queued.append(entry) or True

    await client.download_command(ctx, 21, "dub", 2, 2)
    assert queued[0].fixed_target is saved
    assert client.library.data["routes"]["123:9:77:21"]["channel_id"] == "600"
    guild.create_text_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_refuses_missing_route_without_creating_channels(
    tmp_path, monkeypatch
):
    client, *_ = build(tmp_path, monkeypatch)
    guild, anchor, *_ = server()
    with pytest.raises(ValueError, match="No saved channel"):
        await client.router.resolve(anchor, Job(21, "dub", 1, 1, anchor.id))
    guild.create_text_channel.assert_not_awaited()
    assert not client.library.data.get("routes")


def test_provisional_cache_is_not_presented_as_a_real_title():
    data = {
        "anime": {"21": {"title": "MAL 21"}},
        "jobs": {"id": {"mal": 21, "title": "MAL 21"}},
        "routes": {},
    }
    assert saved_anime_title(data, 21) is None
