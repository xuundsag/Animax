import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from test_app import FakeResolver, add_readbacks, fake_sent

import animax.bot as app
from animax.bot import Job, MegaPlayBot, Settings
from animax.storage import Library
from animax.upload import UploadFailure, upload_episode


class ProgressMessage:
    def __init__(self, id, events):
        self.id = id
        self.author = SimpleNamespace(id=123)
        self.events = events
        self.contents = []
        self.deleted = False
        self.edit = AsyncMock(side_effect=self.edit_impl)
        self.delete = AsyncMock(side_effect=self.delete_impl)
        self.verify_delete = None

    async def edit_impl(self, *, content):
        self.contents.append(content)
        return self

    async def delete_impl(self):
        if self.verify_delete:
            self.verify_delete()
        self.deleted = True
        self.events.append(("progress_deleted", self.id))


def build(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(app, "Resolver", FakeResolver)
    monkeypatch.setattr(app, "fetch_anime_title", AsyncMock(return_value="One Piece"))
    monkeypatch.setattr(
        app,
        "probe",
        AsyncMock(
            return_value={"format": {"duration": "120", "format_name": "mov,mp4"}}
        ),
    )

    async def local_download(stream, work, *args, **kwargs):
        path = work / "whole.mp4"
        path.write_bytes(b"complete fake video")
        (work / "ready.json").write_text(
            json.dumps({"file": path.name, "language": args[0]})
        )
        kwargs["on_progress"]({"fraction": 0.56, "estimated": False})
        return path, None

    monkeypatch.setattr(app, "download", local_download)
    client = MegaPlayBot(
        Settings(delay=0, attempts=1), Library(tmp_path / "anime.json")
    )
    client._connection.user = SimpleNamespace(id=123)
    events = []
    messages = []

    async def status(content):
        message = ProgressMessage(1000 + len(messages), events)
        message.contents.append(content)
        messages.append(message)
        return message

    ctx = SimpleNamespace(
        channel=SimpleNamespace(id=456), send=AsyncMock(side_effect=status)
    )
    target = SimpleNamespace(id=456)
    add_readbacks(target)
    target.upload_files = AsyncMock(
        side_effect=lambda file: [
            SimpleNamespace(cloud=True, filename=file.filename, size=file.size)
        ]
    )

    async def post(content, **kwargs):
        events.append(("video_accepted", content))
        episode = int(re.search(r"`E(\d+)`", content).group(1))
        messages[-1].verify_delete = lambda: (
            pytest.fail("History was not committed before cleanup")
            if not client.library.completed(21, "dub", episode, 123, 456)
            else None
        )
        sent = fake_sent(
            target, content, kwargs["file"], kwargs.get("nonce"), 2000 + episode
        )
        sent.attachments[
            0
        ].url = f"https://cdn.discordapp.com/attachments/456/200{episode}/episode.mp4?ex=123"
        return sent

    target.send = AsyncMock(side_effect=post)
    return client, ctx, target, messages, events


def test_exact_progress_format():
    job = Job(
        21,
        "dub",
        1,
        100,
        456,
        title="One Piece",
        current=1,
        phase="Downloading",
        progress={"fraction": 0.56},
    )
    text = job.episode_text()
    assert text == "**One Piece** | `E1` | Downloading | 56%"
    assert "\n" not in text
    job.progress = {"fraction": 0.9999}
    assert "99%" in job.episode_text()


@pytest.mark.asyncio
async def test_distinct_progress_messages_deleted_only_after_cloud_send_and_commit(
    tmp_path, monkeypatch
):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    job = Job(21, "dub", 1, 3, 456)
    await client.run_job(ctx, target, job, 1_000_000_000, False)
    assert len(messages) == 3 and len(set(m.id for m in messages)) == 3
    assert all(m.deleted for m in messages)
    assert [kind for kind, *_ in events] == ["video_accepted", "progress_deleted"] * 3
    assert target.upload_files.await_count == 3 and target.send.await_count == 3
    assert job.sent == 3 and job.failed == 0
    for episode, message in enumerate(messages, 1):
        assert f"`E{episode}`" in message.contents[0]
        row = client.library.data["anime"]["21"]["episodes"]["dub"][str(episode)][
            "deliveries"
        ]["123:456"]
        assert row["progress_messages"] == []
        assert row["message_id"] == str(2000 + episode)
        report = json.loads(
            (tmp_path / "logs" / f"upload-21-dub-{episode}.json").read_text()
        )
        assert report["method"] == "cloud" and report["message_confirmed"]


@pytest.mark.asyncio
async def test_permission_failure_keeps_status_and_cache(tmp_path, monkeypatch):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    target.upload_files = AsyncMock(
        side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"code": 50013, "message": "Missing Permissions"},
        )
    )
    job = Job(21, "dub", 1, 10, 456)
    await client.run_job(ctx, target, job, 1_000_000_000, False)
    target.send.assert_not_awaited()
    assert len(messages) == 1 and not messages[0].deleted
    assert "| Paused |" in messages[0].contents[-1]
    assert "Discord code" not in messages[0].contents[-1]
    assert not client.library.completed(21, "dub", 1, 123, 456)
    assert (tmp_path / "downloads/123-456-21-dub-1/whole.mp4").exists()
    report = json.loads((tmp_path / "logs/upload-21-dub-1.json").read_text())
    assert report["http_status"] == 403 and report["discord_error_code"] == 50013


@pytest.mark.asyncio
async def test_failed_cleanup_does_not_reclassify_successful_upload(
    tmp_path, monkeypatch
):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    original = ctx.send.side_effect

    async def cannot_delete(content):
        result = await original(content)
        result.delete = AsyncMock(side_effect=RuntimeError("Deletion denied"))
        return result

    ctx.send.side_effect = cannot_delete
    job = Job(21, "dub", 1, 1, 456)
    await client.run_job(ctx, target, job, 1_000_000_000, False)
    assert job.sent == 1 and job.failed == 0
    assert client.library.completed(21, "dub", 1, 123, 456)
    assert not messages[0].deleted


@pytest.mark.asyncio
async def test_only_accepted_download_trigger_is_deleted(tmp_path, monkeypatch):
    client, ctx, _, messages, events = build(tmp_path, monkeypatch)
    guild = SimpleNamespace(id=9, me=SimpleNamespace(id=123))
    permissions = SimpleNamespace(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        attach_files=True,
    )
    target = SimpleNamespace(
        id=456,
        name="episodes",
        guild=guild,
        category_id=77,
        type=discord.ChannelType.text,
        topic=None,
        permissions_for=lambda member: permissions,
    )
    ctx.guild = guild
    ctx.channel = target
    client.channel_for = AsyncMock(return_value=target)
    trigger = ProgressMessage(55, events)
    trigger.author = SimpleNamespace(id=123)
    ctx.message = trigger
    client.launch_job = AsyncMock(return_value=True)
    await client.download_command(ctx, 21, "dub", 1, 1, "resume", "<#456>")
    trigger.delete.assert_awaited_once()
    trigger.delete.reset_mock()
    client.launch_job = AsyncMock(return_value=False)
    await client.download_command(ctx, 21, "dub", 1, 1, "resume", "<#999>")
    trigger.delete.assert_not_awaited()
    with pytest.raises(Exception):
        await client.download_command(ctx, 0, "dub", 1, 1)
    trigger.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_journal_write_failure_does_not_delete_progress_or_resend(
    tmp_path, monkeypatch
):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    original = client.library.record

    def fail_commit(*args, **kwargs):
        if args[6] == "uploaded":
            raise OSError("Simulated disk failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(client.library, "record", fail_commit)
    job = Job(21, "dub", 1, 10, 456)
    await client.run_job(ctx, target, job, 1_000_000_000, False)
    assert (
        target.send.await_count == 1 and len(messages) == 1 and not messages[0].deleted
    )
    assert "| Paused |" in messages[0].contents[-1]
    assert (
        json.loads((tmp_path / "logs/upload-21-dub-1.json").read_text())[
            "message_confirmed"
        ]
        is True
    )


@pytest.mark.asyncio
async def test_confirmation_timeout_is_uncertain_and_not_retried(tmp_path):
    video = tmp_path / "whole.mp4"
    video.write_bytes(b"fake video")

    async def pending(*args, **kwargs):
        await asyncio.sleep(60)

    target = SimpleNamespace(
        id=456,
        upload_files=AsyncMock(return_value=[object()]),
        send=AsyncMock(side_effect=pending),
    )
    add_readbacks(target)
    with pytest.raises(UploadFailure) as caught:
        await upload_episode(
            target,
            video,
            "caption",
            filename="episode.mp4",
            report_path=tmp_path / "upload.json",
            timeout=0.03,
            author_id=123,
        )
    assert caught.value.outcome_unknown
    assert target.send.await_count == 1
    assert video.exists()
    assert json.loads((tmp_path / "upload.json").read_text())["outcome_unknown"]


@pytest.mark.asyncio
async def test_shutdown_during_final_send_requires_review_not_auto_resume(
    tmp_path, monkeypatch
):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    sending = asyncio.Event()

    async def pending(*args, **kwargs):
        sending.set()
        await asyncio.sleep(60)

    target.send = AsyncMock(side_effect=pending)
    job = Job(21, "dub", 1, 10, 456)
    client.library.job(
        job.id,
        account_id="123",
        channel_id="456",
        mal=21,
        language="dub",
        start=1,
        end=10,
        status="running",
    )
    task = asyncio.create_task(client.run_job(ctx, target, job, 1_000_000_000, False))
    await asyncio.wait_for(sending.wait(), 2)
    client.shutting_down = True
    task.cancel()
    await task
    assert client.library.data["jobs"][job.id]["status"] == "needs_review"
    assert client.library.latest_job(123, active_only=True) is None
    assert not messages[0].deleted


@pytest.mark.asyncio
async def test_restart_cleans_progress_for_already_committed_upload(
    tmp_path, monkeypatch
):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    old = ProgressMessage(88, events)
    channel = SimpleNamespace(id=456, fetch_message=AsyncMock(return_value=old))
    monkeypatch.setattr(client, "channel_for", AsyncMock(return_value=channel))
    client.library.record(
        21,
        "One Piece",
        "dub",
        1,
        123,
        456,
        "uploaded",
        message_id="2001",
        progress_messages=[{"channel_id": "456", "message_id": "88"}],
    )
    job = Job(21, "dub", 1, 1, 456)
    await client.run_job(ctx, target, job, 1_000_000_000, False)
    target.send.assert_not_awaited()
    assert old.deleted and job.skipped == 1
    assert (
        client.library.data["anime"]["21"]["episodes"]["dub"]["1"]["deliveries"][
            "123:456"
        ]["progress_messages"]
        == []
    )
