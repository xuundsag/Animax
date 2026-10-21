import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView
from test_debug_release import build

import animax.bot as app
from animax.context import CommandContext
from animax.encoding import video_filter
from animax.settings import FIELDS, Settings, save_setting
from animax.storage import Library


@pytest.mark.asyncio
async def test_settings_list_validation_persistence_and_no_secrets(
    tmp_path, monkeypatch
):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    ctx.send = AsyncMock()
    callback = client.get_command("settings").callback
    await callback(ctx)
    text = ctx.send.await_args.args[0]
    assert text.startswith("```\n") and text.endswith("\n```") and len(text) < 2000
    assert all(name + " =" in text for name in FIELDS)
    assert "TOKEN" not in text
    await callback(ctx, "quality", value="999")
    assert (
        "Valid options:" in ctx.send.await_args.args[0]
        and "1080" in ctx.send.await_args.args[0]
    )
    assert not (tmp_path / "settings.json").exists()
    await callback(ctx, "quality", value="720")
    assert client.cfg.quality == 720
    await callback(ctx, "fps", value="source")
    await callback(ctx, "crf", value="18")
    restored = Settings.from_env(tmp_path / "settings.json")
    assert (restored.quality, restored.frame_rate, restored.video_crf) == (
        720,
        "source",
        18,
    )
    await callback(ctx, "preset", value="ultrafast")
    assert "needs more bits" in ctx.send.await_args.args[0]
    await callback(ctx, "discord_token", value="do-not-store")
    assert "Unknown setting" in ctx.send.await_args.args[0]
    assert "do-not-store" not in (tmp_path / "settings.json").read_text()


@pytest.mark.asyncio
async def test_settings_changes_blocked_while_job_running(tmp_path, monkeypatch):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    ctx.send = AsyncMock()
    client.stream_starting = True
    try:
        await client.get_command("settings").callback(ctx, "crf", value="18")
        assert client.cfg.video_crf == 19 and not (tmp_path / "settings.json").exists()
        assert "Wait for" in ctx.send.await_args.args[0]
    finally:
        client.stream_starting = False


@pytest.mark.parametrize(
    "name,value",
    [
        ("threads", "0"),
        ("threads", "1.5"),
        ("request_interval", "nan"),
        ("fps", "23"),
        ("compress", "maybe"),
        ("keep_downloads", "yesplease"),
        ("log_channel", "-1"),
    ],
)
def test_bad_setting_values(name, value):
    with pytest.raises(ValueError, match="Valid options"):
        FIELDS[name].parse(value)


def test_setting_save_failure_keeps_configuration(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"quality":"1080"}')
    original = path.read_bytes()
    cfg = Settings()

    def fail(*args):
        raise OSError("disk failed")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        save_setting(path, cfg, "quality", "720")
    assert path.read_bytes() == original and cfg.quality == 1080


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delete,success", [(True, True), (True, False), (False, False)]
)
async def test_command_trigger_delete_or_reply(tmp_path, monkeypatch, delete, success):
    client, *_ = build(tmp_path, monkeypatch)
    client.cfg.delete_trigger = delete
    client.safe_delete = AsyncMock(return_value=success)
    msg = SimpleNamespace(
        id=77,
        channel=SimpleNamespace(id=456),
        guild=SimpleNamespace(id=9),
        _state=object(),
    )
    context = CommandContext(message=msg, bot=client, view=StringView(".settings"))
    sender = AsyncMock(return_value=object())
    monkeypatch.setattr(commands.Context, "send", sender)
    await context.send("response")
    await context.send("second")
    await client.on_command_completion(context)
    assert client.safe_delete.await_count == (1 if delete else 0)
    for call in sender.await_args_list:
        if delete and success:
            assert "reference" not in call.kwargs
        else:
            assert call.kwargs["reference"].message_id == 77
            assert not call.kwargs["reference"].fail_if_not_exists
            assert call.kwargs["mention_author"] is False


@pytest.mark.asyncio
async def test_help_has_no_instruction_footer(tmp_path, monkeypatch):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    ctx.send = AsyncMock()
    await client.get_command("help").callback(ctx)
    text = ctx.send.await_args.args[0]
    assert ".help <name>" not in text and text.endswith("```")
    assert "refetch" in text and "settings" in text


def message(mid, episode=1, language="dub", author=123, content=None, attachments=None):
    if attachments is None:
        attachments = [
            SimpleNamespace(
                filename=f"MAL-21_EP-{episode:04d}_{language}.mp4", size=123456
            )
        ]
    return SimpleNamespace(
        id=mid,
        author=SimpleNamespace(id=author),
        guild=SimpleNamespace(id=9),
        content=content or f"**One Piece** | `E{episode}`",
        attachments=attachments,
        jump_url=f"https://discord.com/channels/9/456/{mid}",
    )


def channel(messages):
    async def history(limit):
        assert limit is None
        for item in messages:
            yield item

    return SimpleNamespace(id=456, history=history)


def export_attachment(data):
    raw = json.dumps(data).encode()
    return SimpleNamespace(
        filename="anime.json", size=len(raw), read=AsyncMock(return_value=raw)
    )


@pytest.mark.asyncio
async def test_refetch_live_uploads_ownership_fallback_and_replies(
    tmp_path, monkeypatch
):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    status = SimpleNamespace(edit=AsyncMock())
    ctx.send = AsyncMock(return_value=status)
    target = channel(
        [
            message(
                202, 2, "sub", content="**One Piece** | `E2` | SUB (dub unavailable)"
            ),
            message(201),
            message(300, 3, author=999),
            message(
                301, attachments=[SimpleNamespace(filename="unrelated.mp4", size=10)]
            ),
        ]
    )
    client.channel_for = AsyncMock(return_value=target)
    await client.get_command("refetch").callback(ctx, "<#456>")
    assert client.library.completed(21, "dub", 1, 123, 456)
    assert client.library.completed(21, "dub", 2, 123, 456)
    assert not client.library.completed(21, "dub", 3, 123, 456)
    row = client.library.data["anime"]["21"]["episodes"]["dub"]["2"]["deliveries"][
        "123:456"
    ]
    assert row["actual_language"] == "sub" and row["fallback_reason"]
    assert (
        client.library.previous_message(21, 2, 123, 456, "dub")["message_id"] == "201"
    )
    assert client.library.data["jobs"] == {}
    assert "Refetched 2" in status.edit.await_args.kwargs["content"]
    assert Library(tmp_path / "anime.json").completed(21, "dub", 2, 123, 456)


@pytest.mark.asyncio
async def test_refetch_snapshot_plus_new_uploads_and_latest_message(
    tmp_path, monkeypatch
):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    snapshot = Library(tmp_path / "snapshot/anime.json")
    snapshot.record(
        21,
        "One Piece",
        "dub",
        1,
        123,
        456,
        "uploaded",
        message_id=100,
        jump_url="https://discord.com/channels/9/456/100",
    )
    snapshot.record(
        21,
        "One Piece",
        "dub",
        3,
        123,
        777,
        "uploaded",
        message_id=110,
        jump_url="https://discord.com/channels/9/777/110",
    )
    snapshot.record(21, "One Piece", "dub", 4, 999, 456, "uploaded", message_id=120)
    snapshot.job("running", account_id=123, status="running")
    attachment = export_attachment(snapshot.data)
    target = channel(
        [message(300), message(250, attachments=[attachment]), message(100)]
    )
    status = SimpleNamespace(edit=AsyncMock())
    ctx.send = AsyncMock(return_value=status)
    client.channel_for = AsyncMock(return_value=target)
    await client.get_command("refetch").callback(ctx, "456")
    row = client.library.data["anime"]["21"]["episodes"]["dub"]["1"]["deliveries"][
        "123:456"
    ]
    assert row["message_id"] == "300"
    assert [item["message_id"] for item in row["messages"]] == ["100", "300"]
    assert client.library.completed(21, "dub", 3, 123, 777)
    assert not client.library.completed(21, "dub", 4, 999, 456)
    assert client.library.data["jobs"] == {}


@pytest.mark.asyncio
async def test_refetch_does_not_replace_populated_history(tmp_path, monkeypatch):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    ctx.send = AsyncMock()
    client.library.record(
        21, "One Piece", "dub", 1, 123, 456, "uploaded", message_id=100
    )
    before = client.library.path.read_bytes()
    client.channel_for = AsyncMock()
    await client.get_command("refetch").callback(ctx, "456")
    client.channel_for.assert_not_awaited()
    assert client.library.path.read_bytes() == before
    assert "not empty" in ctx.send.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "timeout", "cancel"])
async def test_refetch_failures_do_not_commit_partial_history(
    tmp_path, monkeypatch, failure
):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    before = client.library.path.read_bytes()
    ready = asyncio.Event()

    async def history(limit):
        yield message(201)
        ready.set()
        if failure == "network":
            raise RuntimeError("connection lost")
        await asyncio.sleep(10)

    client.channel_for = AsyncMock(
        return_value=SimpleNamespace(id=456, history=history)
    )
    status = SimpleNamespace(edit=AsyncMock())
    ctx.send = AsyncMock(return_value=status)
    if failure == "timeout":
        client.cfg.refetch_timeout = 0.02
    task = asyncio.create_task(client.get_command("refetch").callback(ctx, "456"))
    await ready.wait()
    if failure == "cancel":
        client.refetch_task.cancel()
    await task
    assert client.library.path.read_bytes() == before
    assert client.library.data["anime"] == {}
    assert "not changed" in status.edit.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_refetch_skips_invalid_snapshots_and_empty_files_load(
    tmp_path, monkeypatch
):
    path = tmp_path / "anime.json"
    path.write_text("")
    assert Library(path).data["anime"] == {}
    client, ctx, *_ = build(tmp_path, monkeypatch)
    target = channel(
        [
            message(
                500,
                attachments=[
                    export_attachment(
                        {"version": 1, "anime": {"21": {"title": {}, "episodes": []}}}
                    )
                ],
            ),
            message(201),
        ]
    )
    status = SimpleNamespace(edit=AsyncMock())
    ctx.send = AsyncMock(return_value=status)
    client.channel_for = AsyncMock(return_value=target)
    await client.get_command("refetch").callback(ctx, "456")
    assert client.library.completed(21, "dub", 1, 123, 456)


def test_source_fps_default_and_language_caption_styles():
    video = {"width": 1440, "height": 1080, "sample_aspect_ratio": "1:1"}
    sub = video_filter(video, 1080, True, language="sub")
    dub = video_filter(video, 1080, True, language="dub")
    assert "fps=" not in sub and "fps=" not in dub
    assert "BorderStyle=1" in sub and "BackColour=&HFF000000" in sub
    assert "BorderStyle=3" in dub and "BackColour=&H00000000" in dub
    assert "fps=24000/1001" in video_filter(video, 1080, frame_rate="23.976")
    assert Settings().compression_mode == "quality" and not Settings().auto_compress


@pytest.mark.asyncio
async def test_oversized_prepared_video_is_fitted_unless_lossless_is_enabled(
    tmp_path, monkeypatch
):
    client, ctx, target, *_ = build(tmp_path, monkeypatch)
    client.cfg.compression_mode = "quality"
    client.cfg.auto_compress = False

    async def still_too_large(source, *args, **kwargs):
        return source

    fitter = AsyncMock(side_effect=still_too_large)
    monkeypatch.setattr(app, "fit_whole_video", fitter)
    job = app.Job(21, "dub", 1, 1, 456)
    await client.run_job(ctx, target, job, 1, False)
    fitter.assert_awaited_once()
    target.send.assert_not_awaited()
    assert (
        job.failed == 1 and (tmp_path / "downloads/123-456-21-dub-1/whole.mp4").exists()
    )

    client.cfg.lossless = True
    fitter.reset_mock()
    second = app.Job(22, "dub", 1, 1, 456)
    await client.run_job(ctx, target, second, 1, False)
    fitter.assert_not_awaited()


@pytest.mark.asyncio
async def test_settings_channel_is_checked_before_save(tmp_path, monkeypatch):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    ctx.send = AsyncMock()
    client.channel_for = AsyncMock(side_effect=RuntimeError("No access"))
    await client.get_command("settings").callback(ctx, "channel", value="999")
    assert client.cfg.channel_id == 0 and not (tmp_path / "settings.json").exists()
    assert "Settings were not changed" in ctx.send.await_args.args[0]


@pytest.mark.asyncio
async def test_refetch_does_not_overwrite_malformed_nonempty_journal(
    tmp_path, monkeypatch
):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    ctx.send = AsyncMock()
    client.channel_for = AsyncMock()
    for invalid in ({"unrelated": "data"}, {"version": 1, "anime": []}, []):
        original = json.dumps(invalid)
        client.library.path.write_text(original)
        await client.get_command("refetch").callback(ctx, "456")
        assert client.library.path.read_text() == original
        assert "invalid structure" in ctx.send.await_args.args[0]
    client.channel_for.assert_not_awaited()
