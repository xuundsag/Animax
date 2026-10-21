import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import animax.bot as module
from animax.bot import InstanceLock, Job, MegaPlayBot, Settings
from animax.rate_limits import Cooldown, RateLimitError
from animax.storage import Library


class FakeResolver:
    def __init__(self, *args):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    @asynccontextmanager
    async def resolve(self, mal, language, episode):
        stream = SimpleNamespace(episode=episode)
        stream.detach = AsyncMock(return_value=stream)
        yield stream


def add_readbacks(target):
    target.sent_messages = {}

    async def history(*, limit=100, after=None, oldest_first=False):
        messages = sorted(
            target.sent_messages.values(), key=lambda m: m.id, reverse=not oldest_first
        )
        for message in messages[:limit]:
            if after is None or message.id > getattr(after, "id", 0):
                yield message

    target.history = history
    target.fetch_message = AsyncMock(
        side_effect=lambda message_id: target.sent_messages[message_id]
    )


def fake_sent(target, content, attachment, nonce, message_id):
    message = SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=123),
        channel=target,
        content=content,
        nonce=nonce,
        jump_url=f"https://discord.com/channels/1/{target.id}/{message_id}",
        attachments=[
            SimpleNamespace(filename=attachment.filename, size=attachment.size)
        ],
        edit=AsyncMock(),
    )
    target.sent_messages[message_id] = message
    return message


def client(tmp_path, monkeypatch, settings=None):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "Resolver", FakeResolver)
    monkeypatch.setattr(
        module, "fetch_anime_title", AsyncMock(return_value="One Piece")
    )
    monkeypatch.setattr(
        module, "probe", AsyncMock(return_value={"format": {"duration": "120"}})
    )
    bot = MegaPlayBot(settings or Settings(delay=0), Library(tmp_path / "anime.json"))
    bot._connection.user = SimpleNamespace(id=123)
    message = SimpleNamespace(edit=AsyncMock())
    ctx = SimpleNamespace(
        channel=SimpleNamespace(id=456), send=AsyncMock(return_value=message)
    )
    target = SimpleNamespace(id=456)
    add_readbacks(target)

    async def post(content, **kwargs):
        return fake_sent(
            target,
            content,
            kwargs["file"],
            kwargs.get("nonce"),
            999 + len(target.sent_messages),
        )

    target.send = AsyncMock(side_effect=post)
    return bot, ctx, target


def test_library_legacy_backup_and_force_failure(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "1:2:21:dub:1": {"message_ids": [100]},
                "1:2:21:dub:2": {"message_ids": [101, 102]},
            }
        )
    )
    lib = Library(tmp_path / "anime.json")
    assert lib.completed(21, "dub", 1, 1, 2)
    assert not lib.completed(21, "dub", 2, 1, 2)
    lib.record(
        21, "One Piece", "dub", 1, 1, 2, "failed", error={"code": "FAILED_RESEND"}
    )
    assert lib.completed(21, "dub", 1, 1, 2)
    lib.record(
        21, "One Piece", "dub", 3, 1, 2, "uploaded", message_id="103", jump_url="url"
    )
    new = Library(tmp_path / "anime.json")
    assert new.completed(21, "dub", 3, 1, 2)
    assert (tmp_path / "anime.json.bak").exists()
    assert (
        new.data["anime"]["21"]["episodes"]["dub"]["3"]["deliveries"]["1:2"][
            "messages"
        ][0]["message_id"]
        == "103"
    )


def test_defaults_progress_bar():
    assert Settings().request_interval == 0.25
    job = Job(
        21,
        "dub",
        1,
        10,
        456,
        title="One Piece",
        current=2,
        phase="Downloading",
        progress={"fraction": 0.425, "estimated": True, "speed": 1048576},
    )
    assert job.text() == "**One Piece** | `E2` | Downloading | 42%"


def test_single_instance_lock(tmp_path):
    with InstanceLock(tmp_path / ".lock"):
        with pytest.raises(RuntimeError, match="already running"):
            with InstanceLock(tmp_path / ".lock"):
                pass


@pytest.mark.asyncio
async def test_delivery_journal_progress_and_resume(tmp_path, monkeypatch):
    bot, ctx, target = client(tmp_path, monkeypatch)
    seen = []

    async def fake_download(stream, work, *args, **kwargs):
        seen.append(stream.episode)
        kwargs["on_progress"]({"fraction": 0.5, "estimated": True})
        kwargs["on_progress"]({"fraction": 1, "estimated": False})
        path = work / "whole.mp4"
        path.write_bytes(b"video")
        (work / "ready.json").write_text(
            json.dumps({"file": path.name, "language": args[0]})
        )
        return path, None

    monkeypatch.setattr(module, "download", fake_download)
    job = Job(21, "dub", 1, 3, 456)
    await bot.run_job(ctx, target, job, 1_000_000_000, False)
    assert seen == [1, 2, 3] and job.sent == 3
    assert [c.args[0] for c in target.send.await_args_list] == [
        "**One Piece** | `E1`",
        "**One Piece** | `E2`",
        "**One Piece** | `E3`",
    ]
    assert all(
        "file" in c.kwargs and "files" not in c.kwargs
        for c in target.send.await_args_list
    )
    assert bot.library.completed(21, "dub", 3, 123, 456)
    assert len(list((tmp_path / "downloads").iterdir())) == 0
    job2 = Job(21, "dub", 1, 3, 456)
    await bot.run_job(ctx, target, job2, 1_000_000_000, False)
    assert seen == [1, 2, 3] and job2.skipped == 3


@pytest.mark.asyncio
async def test_rate_limit_backs_off_without_episode_hopping(tmp_path, monkeypatch):
    bot, ctx, target = client(
        tmp_path, monkeypatch, Settings(delay=0, rate_limit_retries=1)
    )
    episodes = []
    pauses = []

    async def limited(stream, *args, **kwargs):
        episodes.append(stream.episode)
        raise RateLimitError(800)

    async def pretend_elapsed(ctx, job):
        if bot.cdn_cooldown.remaining() > 0:
            pauses.append(bot.cdn_cooldown.remaining())
        bot.cdn_cooldown.until = 0

    monkeypatch.setattr(module, "download", limited)
    monkeypatch.setattr(bot, "wait_for_cdn", pretend_elapsed)
    job = Job(21, "dub", 1, 100, 456)
    await bot.run_job(ctx, target, job, 1_000_000_000, False)
    assert episodes == [1, 1] and len(pauses) == 1 and pauses[0] > 795
    assert bot.cdn_cooldown.interval == 4
    assert Cooldown(tmp_path / "cdn_cooldown.json").remaining() > 795
    target.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupt_records_resumable_job_without_requests(tmp_path, monkeypatch):
    bot, ctx, target = client(tmp_path, monkeypatch)
    bot.cdn_cooldown.defer(300)
    job = Job(21, "dub", 1, 100, 456)
    bot.library.job(
        job.id,
        account_id="123",
        channel_id="456",
        command_channel_id="456",
        mal=21,
        language="dub",
        start=1,
        end=100,
        status="queued",
    )
    task = asyncio.create_task(bot.run_job(ctx, target, job, 1_000_000_000, False))
    for _ in range(40):
        await asyncio.sleep(0.01)
        if job.phase == "CDN cooldown: no requests":
            break
    assert job.phase == "CDN cooldown: no requests"
    bot.shutting_down = True
    task.cancel()
    await task
    assert bot.library.latest_job(123, active_only=True)["status"] == "interrupted"
    target.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_command_filter(tmp_path, monkeypatch):
    bot, ctx, target = client(tmp_path, monkeypatch)
    bot.process_commands = AsyncMock()
    await bot.on_message(SimpleNamespace(author=SimpleNamespace(id=999)))
    bot.process_commands.assert_not_awaited()
    message = SimpleNamespace(author=SimpleNamespace(id=123))
    await bot.on_message(message)
    bot.process_commands.assert_awaited_once_with(message)
