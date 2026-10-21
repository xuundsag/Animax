import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from test_app import add_readbacks, fake_sent
from test_debug_release import build

import animax.upload as uploader
import animax.upload_verify as verify
from animax.job import Job


def http_error(status):
    cls = discord.DiscordServerError if status >= 500 else discord.HTTPException
    return cls(
        SimpleNamespace(status=status, reason="test error"),
        {"code": 0, "message": "test error"},
    )


@pytest.fixture
def transfer(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "CHECK_DELAYS", (0, 0, 0))
    video = tmp_path / "episode.mp4"
    video.write_bytes(b"complete video")
    target = SimpleNamespace(id=456)
    add_readbacks(target)
    target.upload_files = AsyncMock(
        side_effect=lambda f: [SimpleNamespace(filename=f.filename, size=f.size)]
    )
    events, phases = [], []
    options = dict(
        filename="MAL-21_EP-0003_dub.mp4",
        report_path=tmp_path / "upload.json",
        author_id=123,
        state_path=tmp_path / "state.json",
        on_event=events.append,
        on_phase=phases.append,
    )
    counter = [1000]

    def accept(content, **kwargs):
        counter[0] += 1
        return fake_sent(
            target, content, kwargs["file"], kwargs.get("nonce"), counter[0]
        )

    target.send = AsyncMock(side_effect=accept)
    return target, video, options, events, phases, accept


async def deliver(transfer, **overrides):
    target, video, options, *_ = transfer
    return await uploader.upload_episode(
        target, video, "**One Piece** | `E3`", **(options | overrides)
    )


def report(transfer):
    return json.loads(transfer[2]["report_path"].read_text())


@pytest.mark.asyncio
async def test_every_success_is_fetched_and_verified(transfer):
    sent = await deliver(transfer)
    target, _, _, events, phases, _ = transfer
    target.fetch_message.assert_awaited_once_with(sent.id)
    assert phases == ["Uploading"]
    assert any("Checking" in e for e in events)
    assert (
        report(transfer)["message_confirmed"]
        and report(transfer)["verification"]["state"] == "found"
    )
    assert report(transfer)["account_id"] == "123"


@pytest.mark.asyncio
async def test_500_after_acceptance_recovers_without_resending(transfer):
    target, _, _, _, _, accept = transfer

    def accepted_then_error(content, **kwargs):
        accept(content, **kwargs)
        raise http_error(500)

    target.send.side_effect = accepted_then_error
    sent = await deliver(transfer)
    assert sent.id in target.sent_messages
    assert target.send.await_count == 1
    assert target.upload_files.await_count == 1
    assert report(transfer)["message_confirmed"]


@pytest.mark.asyncio
async def test_delayed_visibility_does_not_trigger_fallback(transfer):
    target, _, _, _, _, accept = transfer
    original_history = target.history
    count = [0]
    pending = []

    async def history(**kwargs):
        count[0] += 1
        if count[0] == 3:
            target.sent_messages[pending[0].id] = pending[0]
        async for message in original_history(**kwargs):
            yield message

    def post(content, **kwargs):
        sent = accept(content, **kwargs)
        pending.append(target.sent_messages.pop(sent.id))
        raise http_error(500)

    target.history = history
    target.send.side_effect = post
    await deliver(transfer)
    assert target.send.await_count == 1 and count[0] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1000, 738_676_633])
async def test_readable_absence_allows_one_fallback_and_reopens_same_file(
    transfer, size
):
    target, video, _, _, phases, accept = transfer
    with video.open("r+b") as file:
        file.truncate(size)
    calls = []

    def post(content, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise http_error(500)
        assert isinstance(kwargs["file"], discord.File)
        assert kwargs["file"].fp.tell() == 0
        assert kwargs["file"].size == size
        return accept(content, **kwargs)

    target.send.side_effect = post
    await deliver(transfer)
    assert target.send.await_count == 2 and target.upload_files.await_count == 1
    assert calls[0]["nonce"] == calls[1]["nonce"]
    assert phases == ["Uploading"]
    assert [a["method"] for a in report(transfer)["attempts"]] == ["cloud", "multipart"]
    assert video.stat().st_size == size


@pytest.mark.asyncio
async def test_multipart_can_fall_back_to_cloud(transfer):
    target, _, _, _, _, accept = transfer

    def post(content, **kwargs):
        if target.send.await_count == 1:
            raise http_error(502)
        return accept(content, **kwargs)

    target.send.side_effect = post
    await deliver(transfer, mode="multipart")
    assert [a["method"] for a in report(transfer)["attempts"]] == ["multipart", "cloud"]


@pytest.mark.asyncio
async def test_fallback_can_be_disabled(transfer):
    target, _, _, _, _, accept = transfer

    def post(content, **kwargs):
        if target.send.await_count == 1:
            raise http_error(500)
        return accept(content, **kwargs)

    target.send.side_effect = post
    await deliver(transfer, fallback=False)
    assert [a["method"] for a in report(transfer)["attempts"]] == ["cloud", "cloud"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 413, 429])
async def test_definitive_errors_are_not_retried(transfer, status):
    target = transfer[0]
    target.send.side_effect = http_error(status)
    with pytest.raises(uploader.UploadFailure) as caught:
        await deliver(transfer)
    assert not caught.value.outcome_unknown
    assert target.send.await_count == 1
    assert report(transfer)["http_status"] == status


@pytest.mark.asyncio
async def test_persistent_500_stops_after_two_attempts(transfer):
    target = transfer[0]
    target.send.side_effect = http_error(500)
    with pytest.raises(uploader.UploadFailure) as caught:
        await deliver(transfer)
    assert caught.value.outcome_unknown
    assert target.send.await_count == 2
    assert report(transfer)["stage"] == "retry_exhausted"
    assert transfer[1].exists()


@pytest.mark.asyncio
async def test_history_failure_after_500_never_proves_absence(transfer):
    target = transfer[0]
    target.send.side_effect = http_error(500)
    old_history = target.history
    calls = [0]

    async def history(**kwargs):
        calls[0] += 1
        if calls[0] > 1:
            raise http_error(403)
        async for message in old_history(**kwargs):
            yield message

    target.history = history
    with pytest.raises(uploader.UploadFailure) as caught:
        await deliver(transfer)
    assert caught.value.outcome_unknown and target.send.await_count == 1


@pytest.mark.asyncio
async def test_history_permission_failure_before_send_sends_nothing(transfer):
    target = transfer[0]

    async def denied(**kwargs):
        raise http_error(403)
        yield

    target.history = denied
    with pytest.raises(uploader.UploadFailure):
        await deliver(transfer)
    target.upload_files.assert_not_awaited()
    target.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_acknowledged_message_missing_is_not_resent(transfer):
    target, _, _, _, _, accept = transfer

    def post(content, **kwargs):
        message = accept(content, **kwargs)
        target.sent_messages.clear()
        return message

    target.send.side_effect = post
    target.fetch_message.side_effect = discord.NotFound(
        SimpleNamespace(status=404, reason="missing"),
        {"code": 10008, "message": "missing"},
    )
    with pytest.raises(uploader.UploadFailure) as caught:
        await deliver(transfer)
    assert caught.value.outcome_unknown and target.send.await_count == 1
    assert report(transfer)["message_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["size", "caption", "duplicates", "limit"])
async def test_conflicts_and_scan_limit_do_not_trigger_resend(
    transfer, monkeypatch, problem
):
    target, _, _, _, _, accept = transfer

    def post(content, **kwargs):
        msg = accept(content, **kwargs)
        if problem == "size":
            msg.attachments[0].size += 1
        elif problem == "caption":
            msg.nonce = "other"
            msg.content = "another upload"
        elif problem == "duplicates":
            accept(content, **kwargs)
        else:
            monkeypatch.setattr(verify, "HISTORY_LIMIT", 1)
            accept("unrelated", **kwargs).attachments = []
            accept("unrelated", **kwargs).attachments = []
        raise http_error(500)

    target.send.side_effect = post
    with pytest.raises(uploader.UploadFailure) as caught:
        await deliver(transfer)
    assert caught.value.outcome_unknown and target.send.await_count == 1


@pytest.mark.asyncio
async def test_verified_intent_recovers_journal_retry_without_reupload(transfer):
    original = await deliver(transfer)
    restored = await deliver(transfer, recover_existing=True)
    assert restored.id == original.id and transfer[0].send.await_count == 1


@pytest.mark.asyncio
async def test_legacy_500_report_can_be_reconciled(transfer):
    target, video, options, _, _, _ = transfer
    legacy = dict(
        started_utc=datetime.now(timezone.utc).isoformat(),
        stage="Posting message",
        method="cloud",
        channel_id="456",
        filename=options["filename"],
        file_size_bytes=video.stat().st_size,
        message_confirmed=False,
        outcome_unknown=True,
    )
    options["report_path"].write_text(json.dumps(legacy))
    await deliver(transfer, recover_existing=True)
    assert target.send.await_count == 1
    assert report(transfer)["message_confirmed"]


@pytest.mark.asyncio
async def test_missing_or_corrupt_prior_state_does_not_send(transfer):
    with pytest.raises(uploader.UploadFailure):
        await deliver(transfer, recover_existing=True)
    transfer[2]["state_path"].write_text("{bad json")
    with pytest.raises(uploader.UploadFailure):
        await deliver(transfer)
    transfer[0].send.assert_not_awaited()
    assert transfer[2]["state_path"].read_text() == "{bad json"


@pytest.mark.asyncio
async def test_cancel_during_verification_keeps_unknown_intent(transfer, monkeypatch):
    started = asyncio.Event()

    async def pending(*args, **kwargs):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(uploader, "verify_upload", pending)
    task = asyncio.create_task(deliver(transfer))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert report(transfer)["outcome_unknown"]
    assert transfer[0].send.await_count == 1


@pytest.mark.asyncio
async def test_bot_verification_and_fallback_are_terminal_only(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "CHECK_DELAYS", (0, 0, 0))
    client, ctx, target, messages, _ = build(tmp_path, monkeypatch)
    client.cfg.channel_id = 456
    events = []
    client.ui.event = lambda text, *args, **kwargs: events.append(text)
    original = target.send.side_effect

    async def post(*args, **kwargs):
        if target.send.await_count == 1:
            raise http_error(500)
        return await original(*args, **kwargs)

    target.send.side_effect = post
    job = Job(21, "dub", 1, 1, 456)
    await client.run_job(ctx, target, job, 1_000_000_000, False)
    assert job.sent == 1 and job.failed == 0
    assert messages[0].contents[-1].endswith("Done | `0.0 MB`")
    assert any("Checking" in event for event in events)
    assert any("retrying cached file" in event for event in events)
    assert not any(
        word in text
        for text in messages[0].contents
        for word in ["Checking destination", "HTTP", "retrying", "Posting message"]
    )


@pytest.mark.asyncio
async def test_intent_is_saved_before_posting(transfer):
    target, _, options, _, _, accept = transfer

    def post(content, **kwargs):
        saved = json.loads(options["state_path"].read_text())
        assert saved["stage"] == "Posting message" and saved["outcome_unknown"]
        assert saved["nonce"] == kwargs["nonce"]
        return accept(content, **kwargs)

    target.send.side_effect = post
    await deliver(transfer)


@pytest.mark.asyncio
async def test_other_author_does_not_count_as_delivery(transfer):
    target, _, _, _, _, accept = transfer

    def post(content, **kwargs):
        sent = accept(content, **kwargs)
        if target.send.await_count == 1:
            sent.author.id = 999
            raise http_error(500)
        return sent

    target.send.side_effect = post
    sent = await deliver(transfer)
    assert sent.author.id == 123 and target.send.await_count == 2


@pytest.mark.asyncio
async def test_storage_failure_before_post_does_not_send(transfer, monkeypatch):
    monkeypatch.setattr(
        uploader,
        "write_report",
        lambda *args: (_ for _ in ()).throw(OSError("disk error")),
    )
    with pytest.raises(uploader.UploadFailure):
        await deliver(transfer)
    transfer[0].send.assert_not_awaited()
    transfer[0].upload_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_report_failure_never_causes_resend(transfer, monkeypatch):
    original = uploader.write_report

    def write(path, data):
        if data.get("message_confirmed"):
            raise OSError("report disk error")
        return original(path, data)

    monkeypatch.setattr(uploader, "write_report", write)
    sent = await deliver(transfer)
    assert sent.id and transfer[0].send.await_count == 1


@pytest.mark.asyncio
async def test_changed_source_during_recovery_is_not_sent(transfer):
    target, video, *_ = transfer

    def post(*args, **kwargs):
        video.write_bytes(b"changed video")
        raise http_error(500)

    target.send.side_effect = post
    with pytest.raises(uploader.UploadFailure) as caught:
        await deliver(transfer)
    assert caught.value.stage == "source_changed"
    assert target.send.await_count == 1


@pytest.mark.asyncio
async def test_verification_timeout_does_not_retry(transfer):
    target, _, _, _, _, accept = transfer

    async def blocked(message_id):
        await asyncio.sleep(60)

    target.fetch_message.side_effect = blocked
    with pytest.raises(uploader.UploadFailure) as caught:
        await deliver(transfer, verify_timeout=0.01)
    assert caught.value.outcome_unknown and target.send.await_count == 1


@pytest.mark.asyncio
async def test_pending_cache_survives_encoding_setting_changes(tmp_path, monkeypatch):
    import animax.media as media

    work = tmp_path / "work"
    work.mkdir()
    source = work / "episode.mp4"
    source.write_bytes(b"video")
    (work / "ready.json").write_text(
        json.dumps(
            {
                "file": source.name,
                "language": "dub",
                "processing_version": 4,
                "quality": 1080,
                "subtitle_mode": "burn",
                "encoder_preset": "fast",
            }
        )
    )
    monkeypatch.setattr(
        media, "probe", AsyncMock(return_value={"format": {"duration": "120"}})
    )
    assert await media.cached_video(work, "dub", 1080, "burn") is None
    cached = await media.cached_video(work, "dub", 1080, "burn", pending_upload=True)
    assert cached[0] == source
    assert (
        await media.cached_video(work, "sub", 1080, "burn", pending_upload=True) is None
    )


@pytest.mark.asyncio
async def test_prior_unknown_bot_send_recovers_without_redownload(
    tmp_path, monkeypatch
):
    import animax.media as media

    monkeypatch.setattr(verify, "CHECK_DELAYS", (0, 0, 0))
    client, ctx, target, messages, _ = build(tmp_path, monkeypatch)
    client.cfg.channel_id = 456
    client.cfg.keep_downloads = True
    original = target.send.side_effect

    async def accepted_but_check_blocked(*args, **kwargs):
        sent = await original(*args, **kwargs)
        target.fetch_message.side_effect = http_error(403)
        return sent

    target.send.side_effect = accepted_but_check_blocked
    first = Job(21, "dub", 1, 1, 456)
    await client.run_job(ctx, target, first, 1_000_000_000, False)
    assert first.failed == 1 and "Paused" in messages[0].contents[-1]
    target.fetch_message.side_effect = lambda mid: target.sent_messages[mid]
    client.channel_for = AsyncMock(
        return_value=SimpleNamespace(
            fetch_message=AsyncMock(
                side_effect=lambda mid: next(m for m in messages if m.id == mid)
            )
        )
    )
    monkeypatch.setattr(
        media, "probe", AsyncMock(return_value={"format": {"duration": "120"}})
    )
    await client.run_job(ctx, target, Job(21, "dub", 1, 1, 456), 1_000_000_000, False)
    assert target.send.await_count == 1
    assert client.library.completed(21, "dub", 1, 123, 456)
    assert all("Done" in message.contents[-1] for message in messages)


@pytest.mark.asyncio
async def test_later_definitive_error_does_not_clear_earlier_uncertainty(transfer):
    target = transfer[0]
    target.send.side_effect = [http_error(500), http_error(413)]
    with pytest.raises(uploader.UploadFailure) as caught:
        await deliver(transfer)
    assert caught.value.outcome_unknown
    assert target.send.await_count == 2
    assert report(transfer)["outcome_unknown"]


@pytest.mark.asyncio
async def test_intentional_new_delivery_is_distinct_from_recovery(transfer):
    first = await deliver(transfer)
    first_nonce = report(transfer)["nonce"]
    second = await deliver(transfer, new_delivery=True)
    assert first.id != second.id
    assert report(transfer)["nonce"] != first_nonce
    assert transfer[0].send.await_count == 2


@pytest.mark.asyncio
async def test_upload_settings_are_validated_and_persisted(tmp_path, monkeypatch):
    from animax.settings import Settings

    client, ctx, *_ = build(tmp_path, monkeypatch)
    ctx.send = AsyncMock()
    callback = client.get_command("settings").callback
    await callback(ctx, "upload_attempts", value="2")
    await callback(ctx, "upload_fallback", value="on")
    await callback(ctx, "upload_check_timeout", value="60")
    cfg = Settings.from_env(tmp_path / "settings.json")
    assert (cfg.upload_attempts, cfg.upload_fallback, cfg.upload_check_timeout) == (
        2,
        True,
        60,
    )
    await callback(ctx, "upload_attempts", value="100")
    assert "Valid options" in ctx.send.await_args.args[0]
    assert client.cfg.upload_attempts == 2


@pytest.mark.asyncio
async def test_empty_history_without_read_permission_is_not_absence(transfer):
    target = transfer[0]
    target.guild = SimpleNamespace(me=SimpleNamespace(id=123))
    target.permissions_for = lambda member: SimpleNamespace(
        view_channel=True, read_message_history=False
    )
    with pytest.raises(uploader.UploadFailure):
        await deliver(transfer)
    target.send.assert_not_awaited()
    target.upload_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_history_permission_after_500_does_not_retry(transfer):
    target = transfer[0]
    permission = SimpleNamespace(view_channel=True, read_message_history=True)
    target.guild = SimpleNamespace(me=SimpleNamespace(id=123))
    target.permissions_for = lambda member: permission

    def post(*args, **kwargs):
        permission.read_message_history = False
        raise http_error(500)

    target.send.side_effect = post
    with pytest.raises(uploader.UploadFailure) as caught:
        await deliver(transfer)
    assert caught.value.outcome_unknown and target.send.await_count == 1


@pytest.mark.asyncio
async def test_missing_member_cache_uses_bounded_member_lookup(transfer):
    target = transfer[0]
    target.guild = SimpleNamespace(
        me=None, fetch_member=AsyncMock(return_value=SimpleNamespace(id=123))
    )
    target.permissions_for = lambda member: SimpleNamespace(
        view_channel=True, read_message_history=True
    )
    await deliver(transfer)
    assert target.guild.fetch_member.await_count >= 1
    assert target.send.await_count == 1
