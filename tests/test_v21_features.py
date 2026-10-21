import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_debug_release import build

import animax.bot as app
import animax.reset as reset
from animax.job import Job
from animax.resolver import Stream, choose_caption
from animax.settings import Settings
from animax.storage import Library


def seed(library):
    library.record(21, "One Piece", "dub", 1, 123, 456, "uploaded", message_id="88")
    library.job("old-job", account_id="123", mal=21, language="dub", start=1, end=100)
    root = library.path.parent
    (root / "downloads").mkdir(exist_ok=True)
    (root / "downloads" / "episode.mp4").write_bytes(b"cached video")
    (root / "state.json").write_text('{"123:456:21:dub:1":{"message_ids":[88]}}')
    (root / ".env").write_text("DISCORD_TOKEN=not-a-real-token")
    (root / "cdn_cooldown.json").write_text('{"until":99999999999}')


@pytest.mark.asyncio
async def test_reset_confirmation_then_download_episode_one_again(
    tmp_path, monkeypatch
):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    seed(client.library)
    original_send = ctx.send
    ctx.send = AsyncMock()
    callback = client.get_command("reset").callback
    await callback(ctx)
    token = client.reset_request.token
    assert client.library.completed(21, "dub", 1, 123, 456)
    assert (tmp_path / "downloads/episode.mp4").exists()
    assert f".reset confirm {token}" in ctx.send.await_args.args[0]
    await callback(ctx, "confirm", token)
    assert client.library.data == {"version": 1, "anime": {}, "jobs": {}}
    assert not (tmp_path / "downloads").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "anime.json.bak").exists()
    assert Library(tmp_path / "anime.json").data["anime"] == {}
    assert (tmp_path / ".env").read_text() == "DISCORD_TOKEN=not-a-real-token"
    assert (
        json.loads((tmp_path / "cdn_cooldown.json").read_text())["until"] == 99999999999
    )
    assert not list(tmp_path.glob(".reset-pending-*"))
    await callback(ctx, "confirm", token)
    assert "invalid or expired" in ctx.send.await_args.args[0]
    ctx.send = original_send
    job = Job(21, "dub", 1, 1, 456)
    await client.run_job(ctx, target, job, 10**9, False)
    assert job.sent == 1 and not job.skipped
    assert target.send.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["wrong_code", "wrong_channel", "expired", "cancel"])
async def test_reset_rejects_unconfirmed_requests(tmp_path, monkeypatch, reason):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    seed(client.library)
    ctx.send = AsyncMock()
    callback = client.get_command("reset").callback
    await callback(ctx)
    token = client.reset_request.token
    if reason == "wrong_code":
        token = "incorrect"
    elif reason == "wrong_channel":
        ctx.channel.id = 999
    elif reason == "expired":
        original = client.reset_request
        client.reset_request = reset.ResetRequest(
            original.account_id, original.channel_id, token, 0
        )
    elif reason == "cancel":
        await callback(ctx, "cancel")
    await callback(ctx, "confirm", token)
    assert client.library.completed(21, "dub", 1, 123, 456)
    assert (tmp_path / "downloads/episode.mp4").exists()


@pytest.mark.asyncio
async def test_reset_rejects_running_job(tmp_path, monkeypatch):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    seed(client.library)
    ctx.send = AsyncMock()
    client.stream_starting = True
    try:
        await client.get_command("reset").callback(ctx)
        assert "wait for all jobs to stop" in ctx.send.await_args.args[0]
        assert client.reset_request is None
        assert client.library.completed(21, "dub", 1, 123, 456)
    finally:
        client.stream_starting = False


def test_reset_rolls_back_journal_failure(tmp_path, monkeypatch):
    library = Library(tmp_path / "anime.json")
    seed(library)
    monkeypatch.setattr(
        library, "save", lambda: (_ for _ in ()).throw(OSError("disk failure"))
    )
    with pytest.raises(OSError):
        reset.reset_history(tmp_path, library)
    assert library.completed(21, "dub", 1, 123, 456)
    assert Library(tmp_path / "anime.json").completed(21, "dub", 1, 123, 456)
    assert (tmp_path / "downloads/episode.mp4").exists()
    assert (tmp_path / "state.json").exists()


def test_reset_recovers_interrupted_file_staging(tmp_path):
    library = Library(tmp_path / "anime.json")
    seed(library)
    folder = tmp_path / ".reset-pending-interrupted"
    folder.mkdir()
    reset.write_manifest(
        folder, {"phase": "staging", "paths": ["anime.json", "downloads", "state.json"]}
    )
    (tmp_path / "anime.json").replace(folder / "anime.json")
    (tmp_path / "downloads").replace(folder / "downloads")
    reset.recover_resets(tmp_path)
    assert Library(tmp_path / "anime.json").completed(21, "dub", 1, 123, 456)
    assert (tmp_path / "downloads/episode.mp4").exists()


def test_reset_committed_cleanup_failure_does_not_restore_history(
    tmp_path, monkeypatch
):
    library = Library(tmp_path / "anime.json")
    seed(library)
    original = reset.shutil.rmtree

    def fail(path):
        raise OSError("cannot delete yet")

    monkeypatch.setattr(reset.shutil, "rmtree", fail)
    assert reset.reset_history(tmp_path, library) is False
    assert not library.completed(21, "dub", 1, 123, 456)
    monkeypatch.setattr(reset.shutil, "rmtree", original)
    reset.recover_resets(tmp_path)
    assert Library(tmp_path / "anime.json").data["anime"] == {}
    assert not list(tmp_path.glob(".reset-pending-*"))


def test_reset_refuses_symlink_cache(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    library = Library(root / "anime.json")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.mp4").write_bytes(b"keep")
    (root / "downloads").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        reset.reset_history(root, library)
    assert (outside / "keep.mp4").read_bytes() == b"keep"


def test_profile_ignores_obsolete_v2_settings(monkeypatch):
    monkeypatch.setenv("QUALITY", "720")
    monkeypatch.setenv("ENCODER_PRESET", "ultrafast")
    cfg = Settings.from_env()
    assert (
        cfg.quality,
        cfg.encoder_preset,
        cfg.encode_threads,
        cfg.target_mb,
        cfg.output_max_mb,
    ) == (1080, "veryfast", 4, 450, 950)
    monkeypatch.setenv("ENCODE_THREADS", "0")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_enhancing_elapsed_minutes_seconds(monkeypatch):
    monkeypatch.setattr("animax.job.time.monotonic", lambda: 101.0)
    job = Job(
        21, "dub", 1, 1, 456, title="One Piece", phase="Enhancing", stage_started=100
    )
    assert job.episode_text() == "**One Piece** | `E1` | Enhancing | `0:1`"
    job.stage_started = 40
    assert job.episode_text().endswith("Enhancing | `1:1`")


@pytest.mark.asyncio
async def test_detached_source_preserves_caption_bytes_and_cookies():
    raw = b"WEBVTT\n\n00:00.000 --> 00:02.000\n- K- K- King of the Pirates?!\n- Yep.\n"
    tracks = [
        {
            "kind": "subtitles",
            "label": "English",
            "file": "https://example.test/sub.vtt",
            "default": True,
        },
        {
            "kind": "captions",
            "label": "English CC (Dub)",
            "file": "https://example.test/dub.vtt",
        },
    ]
    context = SimpleNamespace(cookies=AsyncMock(return_value=[{"name": "cookie"}]))
    stream = Stream(
        "https://example.test/video.m3u8",
        {"Referer": "https://example.test/"},
        tracks,
        context,
    )
    stream.caption_bytes = AsyncMock(return_value=raw)
    source = await stream.detach("dub")
    stream.caption_bytes.assert_awaited_once_with(tracks[1])
    assert await source.caption_bytes(tracks[1]) == raw
    assert await source.cookies() == [{"name": "cookie"}]
    assert not hasattr(source, "context")
    assert choose_caption(tracks, "sub") == tracks[0]


@pytest.mark.asyncio
async def test_browser_closed_before_download_and_no_caption_edit(
    tmp_path, monkeypatch
):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    closed = []
    sent = []
    original_download = app.download
    original_send = target.send.side_effect

    class Resolver:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        @asynccontextmanager
        async def resolve(self, *args):
            stream = SimpleNamespace(episode=1)
            stream.detach = AsyncMock(return_value=stream)
            yield stream

    async def download(*args, **kwargs):
        assert closed == [True]
        assert (
            kwargs["quality"] == 1080
            and kwargs["encode_threads"]
            == client.scheduler.resources.plan.encoder_threads
        )
        return await original_download(*args, **kwargs)

    async def send(*args, **kwargs):
        message = await original_send(*args, **kwargs)
        sent.append(message)
        return message

    monkeypatch.setattr(app, "Resolver", Resolver)
    monkeypatch.setattr(app, "download", download)
    target.send.side_effect = send
    await client.run_job(ctx, target, Job(21, "dub", 1, 1, 456), 10**9, False)
    assert target.send.await_args.args[0] == "**One Piece** | `E1`"
    sent[0].edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_callbacks_status_history_search_cancel_resume(
    tmp_path, monkeypatch
):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    ctx.send = AsyncMock()
    await client.get_command("status").callback(ctx)
    assert "Queued: 0" in ctx.send.await_args.args[0]
    client.job = Job(21, "dub", 1, 1, 456, title="One Piece")
    await client.get_command("status").callback(ctx)
    assert "Shared encode threads" in ctx.send.await_args.args[0]
    await client.get_command("resume").callback(ctx)
    assert "No matching resumable job" in ctx.send.await_args.args[0]
    client.library.record(
        21,
        "One Piece",
        "dub",
        1,
        123,
        456,
        "uploaded",
        message_id=88,
        jump_url="https://discord.com/channels/1/456/88",
    )
    await client.get_command("history").callback(ctx, 21)
    assert (
        "One Piece" in ctx.send.await_args.args[0]
        and "1/1" in ctx.send.await_args.args[0]
    )
    await client.get_command("search").callback(ctx, query="eisode 1 onepimse")
    assert (
        ctx.send.await_args.args[0]
        == "One Piece `E1` : [Click here](https://discord.com/channels/1/456/88)"
    )
    await client.get_command("search").callback(ctx, query="episode 9999 One Piece")
    assert ctx.send.await_args.args[0] == "No matching uploaded episodes."
    await client.get_command("help").callback(ctx, "notacommand")
    assert ctx.send.await_args.args[0] == "Unknown command. Use .help."
    await client.get_command("cancel").callback(ctx)
    assert "Use `.cancel JOB_ID`" in ctx.send.await_args.args[0]
    client.scheduler.cancel = AsyncMock(return_value="Cancelled")
    await client.get_command("cancel").callback(ctx, "job-123")
    client.scheduler.cancel.assert_awaited_once_with("job-123")
    client.library.job(
        "saved", account_id=123, channel_id=456, mal=21, language="dub", start=1, end=1
    )
    client.resume_record = AsyncMock()
    await client.get_command("resume").callback(ctx)
    client.resume_record.assert_awaited_once()


@pytest.mark.asyncio
async def test_getlist_streams_stable_file_across_atomic_journal_update(
    tmp_path, monkeypatch
):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    before = client.library.path.read_bytes()
    received = []

    async def send(*args, **kwargs):
        assert not args and list(kwargs) == ["file"]
        attachment = kwargs["file"]
        assert not isinstance(attachment.fp, __import__("io").BytesIO)
        client.library.record(
            21, "One Piece", "dub", 1, 123, 456, "uploaded", message_id=88
        )
        received.append(attachment.fp.read())

    ctx.send = AsyncMock(side_effect=send)
    await client.get_command("getlist").callback(ctx)
    assert received == [before]
    assert client.library.path.read_bytes() != before
