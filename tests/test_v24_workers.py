import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from test_debug_release import build
from test_v22_features import message

from animax import media
from animax.job import Job
from animax.rate_limits import Cooldown
from animax.refetch import scope_channels
from animax.resources import GiB, Plan, Resources, plan_capacity
from animax.routing import LogContext
from animax.scheduler import Entry
from animax.settings import Settings, read_overrides, save_setting
from animax.storage import Library


def roomy(client, monkeypatch):
    manager = client.scheduler
    monkeypatch.setattr(manager, "configure", lambda: None)
    manager.resources.plan = Plan(6, 6, 1)
    monkeypatch.setattr(manager.resources, "admission_reason", lambda: "")
    manager.resources.cfg = replace(client.cfg, request_interval=0)
    return manager


def server():
    permissions = NS(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        attach_files=True,
    )
    guild = NS(id=9, me=NS(id=123))
    category = NS(id=77)
    channels = [category]

    def channel(cid, name, cat=77, topic=None, history_messages=()):
        async def history(limit=None):
            for item in history_messages:
                yield item

        item = NS(
            id=cid,
            name=name,
            guild=guild,
            category_id=cat,
            category=category if cat else None,
            topic=topic,
            type=discord.ChannelType.text,
            overwrites={"privacy": "retained"},
            permissions_for=lambda member: permissions,
            history=history,
            send=AsyncMock(return_value=NS(id=700, edit=AsyncMock())),
        )
        channels.append(item)
        return item

    anchor = channel(456, "logs")
    guild.fetch_channels = AsyncMock(side_effect=lambda: list(channels))

    async def create(name, **kwargs):
        await asyncio.sleep(0)
        made = channel(
            500 + len(channels),
            name,
            getattr(kwargs["category"], "id", None),
            kwargs["topic"],
        )
        assert kwargs["overwrites"] == anchor.overwrites
        return made

    guild.create_text_channel = AsyncMock(side_effect=create)
    return guild, anchor, channels, channel


@pytest.mark.parametrize(
    "threads,cpus,memory,expected_threads",
    [
        (6, 8, 8 * GiB, 6),
        (6, 2, 8 * GiB, 2),
        (6, 8, GiB, 6),
    ],
)
def test_shared_capacity_plan_is_serial(threads, cpus, memory, expected_threads):
    plan = plan_capacity(
        Settings(encode_threads=threads, max_temp_bytes=32 * GiB),
        dict(cpu_slots=cpus, available_memory_bytes=memory, free_disk_bytes=64 * GiB),
    )
    assert plan.active_jobs == 1
    assert plan.encoder_threads == expected_threads
    assert plan.encoder_lanes == plan.download_lanes == plan.upload_lanes == 1


def test_env_and_saved_channel_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("CHANNEL_ID", "456")
    monkeypatch.delenv("LOG_CHANNEL_ID", raising=False)
    assert Settings.from_env().channel_id == 456
    monkeypatch.setenv("LOG_CHANNEL_ID", "789")
    assert Settings.from_env().channel_id == 789
    path = tmp_path / "settings.json"
    path.write_text('{"channel": "456", "threads": "6", "workers": 3}')
    cfg = Settings.from_env(path)
    assert cfg.channel_id == 456 and cfg.encode_threads == 6
    assert read_overrides(path) == {"log_channel": "456", "threads": "6"}
    save_setting(path, cfg, "threads", "3")
    data = json.loads(path.read_text())
    assert data["log_channel"] == "456" and "channel" not in data
    assert data["threads"] == "3" and "workers" not in data
    with pytest.raises(KeyError):
        save_setting(path, cfg, "workers", "3")
    path.write_text('{"channel": "456", "log_channel": "789"}')
    assert read_overrides(path) == {"log_channel": "789"}


def test_worker_label_removed_and_cooldown_does_not_relax_early(tmp_path):
    job = Job(21, "dub", 3, 5, 456, title="One Piece", worker_id=2)
    assert job.episode_text().startswith("**One Piece** | `E3`")
    assert "W2" not in job.episode_text()
    cooldown = Cooldown(tmp_path / "cooldown.json")
    cooldown.slow_down()
    cooldown.defer(30)
    before = cooldown.path.read_bytes()
    cooldown.relax()
    assert cooldown.path.read_bytes() == before


@pytest.mark.asyncio
async def test_many_jobs_are_serial_and_keep_episode_order(tmp_path, monkeypatch):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    manager = roomy(client, monkeypatch)
    active = 0
    peak_active = 0
    episodes = {}

    async def run(entry):
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        job = entry.job
        try:
            for episode in range(1, 4):
                for stage in ("transfer", "encode", "upload"):
                    async with manager.resources.lease(stage, job):
                        await asyncio.sleep(0.001)
                episodes.setdefault(job.id, []).append(episode)
            client.library.job(job.id, status="finished")
        finally:
            active -= 1

    client.run_managed_job = run
    jobs = [Job(21 if n < 10 else n + 100, "dub", 1, 3, 456) for n in range(80)]
    for job in jobs:
        assert manager.enqueue(Entry(ctx, ctx.channel, job, False))
    assert len(manager.queue) == len(jobs) and not manager.active
    assert not manager.tasks
    dispatcher = manager.dispatcher
    await asyncio.wait_for(dispatcher, 12)
    assert peak_active == 1
    assert manager.resources.peaks == {"transfer": 1, "encode": 1, "upload": 1}
    assert manager.resources.occupied == dict.fromkeys(manager.resources.gates, 0)
    assert all(episodes[job.id] == [1, 2, 3] for job in jobs)
    assert all(
        record["status"] == "finished"
        for record in Library(client.library.path).data["jobs"].values()
    )
    assert not manager.busy()


@pytest.mark.asyncio
async def test_cancel_queue_active_and_release_resources(tmp_path, monkeypatch):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    manager = roomy(client, monkeypatch)
    started = asyncio.Event()

    async def run(entry):
        async with manager.resources.lease("encode", entry.job):
            started.set()
            await asyncio.Event().wait()

    client.run_managed_job = run
    for mal in range(1, 6):
        manager.enqueue(Entry(ctx, ctx.channel, Job(mal, "dub", 1, 1, 456), False))
    await asyncio.wait_for(started.wait(), 2)
    assert "1 active and 4 queued" in await manager.cancel("all")
    if manager.dispatcher:
        await asyncio.wait_for(manager.dispatcher, 2)
    assert not manager.busy()
    assert manager.resources.occupied["encode"] == 0
    assert all(
        record["status"] == "cancelled"
        for record in client.library.data["jobs"].values()
    )


@pytest.mark.asyncio
async def test_cancelled_unstarted_job_releases_scheduler_slot(tmp_path, monkeypatch):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    manager = roomy(client, monkeypatch)
    job = Job(21, "dub", 1, 1, 456)
    entry = Entry(ctx, ctx.channel, job, False)
    client.library.job(job.id, status="running")
    task = asyncio.create_task(manager._run(1, entry))
    manager.active[1] = entry
    manager.tasks[1] = task
    task.add_done_callback(lambda done: manager._finished(1, entry, done))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not manager.active and not manager.tasks
    assert client.library.data["jobs"][job.id]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_shutdown_keeps_queue_and_interrupts_active(tmp_path, monkeypatch):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    manager = roomy(client, monkeypatch)
    started = asyncio.Event()

    async def run(entry):
        started.set()
        await asyncio.Event().wait()

    client.run_managed_job = run
    for mal in (21, 22):
        manager.enqueue(Entry(ctx, ctx.channel, Job(mal, "dub", 1, 1, 456), False))
    await started.wait()
    client.shutting_down = True
    await manager.close()
    assert sorted(
        r["status"] for r in Library(client.library.path).data["jobs"].values()
    ) == ["interrupted", "queued"]
    assert not manager.tasks and not manager.busy()


@pytest.mark.asyncio
async def test_admission_wait_and_maintenance_guards(tmp_path, monkeypatch):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    manager = roomy(client, monkeypatch)
    monkeypatch.setattr(
        manager.resources, "admission_reason", lambda: "Waiting for free disk space"
    )
    client.run_managed_job = AsyncMock()
    manager.enqueue(Entry(ctx, ctx.channel, Job(21, "dub", 1, 1, 456), False))
    await asyncio.sleep(0)
    assert client.busy() and "Waiting for free disk" in manager.status_text()
    await client.get_command("settings").callback(ctx, "threads", value="6")
    assert "Wait" in ctx.send.await_args.args[0]
    await client.get_command("reset").callback(ctx)
    assert ".cancel all" in ctx.send.await_args.args[0]
    await client.get_command("refetch").callback(ctx)
    assert "Wait" in ctx.send.await_args.args[0]
    client.run_managed_job.assert_not_awaited()
    await manager.cancel("all")
    if manager.dispatcher:
        await manager.dispatcher


def test_journal_failure_rolls_back_job_and_review_is_not_complete(
    tmp_path, monkeypatch
):
    library = Library(tmp_path / "anime.json")
    library.record(21, "One Piece", "dub", 1, 123, 456, "uploaded", message_id=999)
    library.record(
        21, "One Piece", "dub", 1, 123, 456, "needs_review", needs_review=True
    )
    assert not library.completed(21, "dub", 1, 123, 456)
    before = copy.deepcopy(library.data)
    monkeypatch.setattr(library, "save", lambda: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        library.job("new", status="queued")
    assert library.data == before


@pytest.mark.asyncio
async def test_manual_route_persists_and_reuses_selected_channel(tmp_path, monkeypatch):
    client, *_ = build(tmp_path, monkeypatch)
    guild, anchor, _, channel = server()
    target = channel(600, "episodes")
    job = Job(21, "dub", 1, 1, target.id, title="One Piece")
    first, second = await asyncio.gather(
        client.router.remember_target(target, job, anchor),
        client.router.remember_target(target, job, anchor),
    )
    assert first is second is target
    client.channel_for = AsyncMock(return_value=target)
    assert (
        await client.router.saved_target(guild.id, job.mal, preferred_category=77)
        is target
    )
    assert client.library.data["routes"]["123:9:77:21"]["channel_id"] == "600"
    assert client.library.data["routes"]["123:9:77:21"]["route_version"] == 3
    guild.create_text_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_saved_routes_choose_scope_and_refuse_ambiguous_channels(
    tmp_path, monkeypatch
):
    client, *_ = build(tmp_path, monkeypatch)
    guild, anchor, _, channel = server()
    in_scope = channel(600, "manual-one", cat=77)
    elsewhere = channel(601, "manual-two", cat=88)

    def route(category, target):
        return {
            "account_id": "123",
            "guild_id": str(guild.id),
            "category_id": str(category),
            "mal": 21,
            "channel_id": str(target.id),
        }

    client.library.data["routes"] = {
        "123:9:77:21": route(77, in_scope),
        "123:9:88:21": route(88, elsewhere),
    }
    client.channel_for = AsyncMock(
        side_effect=lambda cid: {"600": in_scope, "601": elsewhere}[str(cid)]
    )
    assert (
        await client.router.saved_target(guild.id, 21, preferred_category=77)
        is in_scope
    )
    with pytest.raises(ValueError, match="multiple saved upload channels"):
        await client.router.saved_target(guild.id, 21)
    wrong = channel(602, "marked", topic="ANIMAX MAL:22")
    with pytest.raises(ValueError, match="marked for another MAL"):
        await client.router.remember_target(
            wrong, Job(21, "dub", 1, 1, wrong.id), anchor
        )


@pytest.mark.asyncio
async def test_missing_saved_route_never_creates_a_channel(tmp_path, monkeypatch):
    client, *_ = build(tmp_path, monkeypatch)
    guild, anchor, *_ = server()
    job = Job(21, "dub", 1, 1, 456, title="One Piece")
    with pytest.raises(ValueError, match="No saved channel"):
        await client.router.resolve(anchor, job)
    guild.create_text_channel.assert_not_awaited()
    assert not client.library.data.get("routes")


@pytest.mark.asyncio
async def test_preferred_logs_manual_route_and_legacy_resume(tmp_path, monkeypatch):
    client, *_ = build(tmp_path, monkeypatch)
    guild, anchor, _, channel = server()
    target = channel(600, "upload")
    ctx = NS(guild=guild, channel=anchor, send=AsyncMock(), message=None)
    client.cfg.channel_id = anchor.id
    client.cfg.delete_trigger = False

    async def channel_for(channel_id):
        return anchor if str(channel_id) == str(anchor.id) else target

    client.channel_for = AsyncMock(side_effect=channel_for)
    seen = []
    client.scheduler.enqueue = lambda entry: seen.append(entry) or True
    await client.download_command(ctx, 21, "dub", 1, 2, "resume", "<#600>")
    assert seen[0].ctx.channel is anchor and seen[0].fixed_target is target
    await seen[0].ctx.send("download status")
    assert ctx.send.await_args.args[0] == "download status"

    record = dict(
        id="legacy",
        mal=21,
        language="dub",
        start=1,
        end=2,
        channel_id="600",
        anchor_channel_id="456",
        log_channel_id="456",
        routed=True,
        title="One Piece",
        status="interrupted",
    )
    client.launch_job = AsyncMock(return_value=True)
    await client.resume_record(record)
    call = client.launch_job.await_args
    assert call.kwargs["fixed_target"] is target and call.args[3] is False
    assert call.kwargs["notify"] is False
    other = NS(id=999, send=AsyncMock())
    await LogContext(other, ctx).send("log only")
    other.send.assert_awaited_once_with("log only")


@pytest.mark.asyncio
async def test_auto_resume_all_and_no_duplicate_enqueue(tmp_path, monkeypatch):
    client, *_ = build(tmp_path, monkeypatch)
    monkeypatch.setattr(client.scheduler, "configure", lambda: None)
    for i, status in enumerate(
        ("queued", "running", "interrupted", "finished", "needs_review")
    ):
        client.library.job(str(i), account_id="123", status=status)
    client.resume_record = AsyncMock()
    await client.on_ready()
    assert {call.args[0]["id"] for call in client.resume_record.await_args_list} == {
        "0",
        "1",
        "2",
    }
    await client.on_ready()
    assert client.resume_record.await_count == 3


@pytest.mark.asyncio
async def test_category_refetch_then_guild_scope_and_route_rebuild(
    tmp_path, monkeypatch
):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    guild, anchor, _, channel = server()
    first = channel(600, "one-piece", history_messages=[message(901)])
    channel(601, "outside", cat=88, history_messages=[message(902, episode=2)])
    hidden = channel(602, "hidden")
    hidden.permissions_for = lambda member: NS(
        view_channel=False, read_message_history=False
    )
    client.channel_for = AsyncMock(return_value=anchor)
    ctx.send = AsyncMock(return_value=NS(edit=AsyncMock()))
    await client.get_command("refetch").callback(ctx)
    assert client.library.completed(21, "dub", 1, 123, first.id)
    assert not client.library.completed(21, "dub", 2, 123, 601)
    assert client.library.data["routes"]["123:9:77:21"]["channel_id"] == "600"
    anchor.category_id = None
    selected, skipped = await scope_channels(anchor, 123)
    assert {c.id for c in selected} == {456, 600, 601} and skipped == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "cancel"])
async def test_refetch_scope_failure_has_no_partial_commit(
    tmp_path, monkeypatch, failure
):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    _, anchor, _, channel = server()
    channel(600, "one-piece", history_messages=[message(901)])
    bad = channel(601, "bad")

    async def history(limit=None):
        raise asyncio.CancelledError if failure == "cancel" else RuntimeError("network")
        yield

    bad.history = history
    client.channel_for = AsyncMock(return_value=anchor)
    ctx.send = AsyncMock(return_value=NS(edit=AsyncMock()))
    before = client.library.path.read_bytes()
    await client.get_command("refetch").callback(ctx)
    assert client.library.path.read_bytes() == before
    assert not client.library.data["anime"] and not client.library.data.get("routes")


@pytest.mark.asyncio
async def test_media_releases_transfer_before_waiting_for_encoder(
    tmp_path, monkeypatch
):
    cfg = Settings(request_interval=0)
    resources = Resources(tmp_path, cfg)
    encoder_started = asyncio.Event()
    second_download = asyncio.Event()
    overlap = []
    info = dict(
        format={"duration": "2", "format_name": "mp4"},
        streams=[
            {
                "codec_type": "video",
                "width": 320,
                "height": 180,
                "sample_aspect_ratio": "1:1",
                "avg_frame_rate": "24/1",
            }
        ],
    )
    monkeypatch.setattr(media, "probe", AsyncMock(return_value=info))
    monkeypatch.setattr(media, "write_cookie_file", AsyncMock())

    async def process(args, work, **kwargs):
        if "downloader.py" in str(args[1]):
            assert resources.occupied["transfer"] == 1
            (work / "source.mp4").write_bytes(b"local fixture")
            if work.name == "second":
                second_download.set()
        else:
            assert resources.occupied["encode"] == 1
            if work.name == "first":
                encoder_started.set()
                await asyncio.wait_for(second_download.wait(), 2)
                overlap.append(True)
            (work / "episode.mp4").write_bytes(b"encoded local fixture")

    monkeypatch.setattr(media, "run_process", process)

    async def run(name):
        job = Job(21 if name == "first" else 22, "sub", 1, 1, 456)
        async with resources.lease("transfer", job) as lease:
            return await media.download(
                NS(url="https://example.invalid", headers={}, tracks=[]),
                tmp_path / name,
                "sub",
                subtitle_mode="none",
                resources=resources,
                job=job,
                transfer_lease=lease,
            )

    first = asyncio.create_task(run("first"))
    await asyncio.wait_for(encoder_started.wait(), 2)
    second = asyncio.create_task(run("second"))
    await asyncio.gather(first, second)
    assert overlap == [True] and not any(resources.occupied.values())


@pytest.mark.asyncio
async def test_upload_slot_covers_verification_before_next_video(tmp_path, monkeypatch):
    import animax.bot as app

    client, ctx, target, *_ = build(tmp_path, monkeypatch)
    first_upload = asyncio.Event()
    second_ready = asyncio.Event()
    allow_confirmation = asyncio.Event()
    calls = []
    original_download = app.download

    async def download(*args, **kwargs):
        result = await original_download(*args, **kwargs)
        if "-22-" in str(args[1]):
            second_ready.set()
        return result

    async def upload(target, source, caption, **kwargs):
        calls.append(kwargs["filename"])
        assert client.scheduler.resources.occupied["upload"] == 1
        if len(calls) == 1:
            first_upload.set()
            await allow_confirmation.wait()  # Simulates read-back/recovery after POST.
        return NS(id=900 + len(calls), jump_url=None)

    monkeypatch.setattr(app, "download", download)
    monkeypatch.setattr(app, "upload_episode", upload)
    first = asyncio.create_task(
        client.run_job(
            ctx, target, Job(21, "dub", 1, 1, 456, worker_id=1), 10**9, False
        )
    )
    await asyncio.wait_for(first_upload.wait(), 2)
    second = asyncio.create_task(
        client.run_job(
            ctx, target, Job(22, "dub", 1, 1, 456, worker_id=2), 10**9, False
        )
    )
    await asyncio.wait_for(second_ready.wait(), 2)
    assert len(calls) == 1
    allow_confirmation.set()
    await asyncio.wait_for(asyncio.gather(first, second), 3)
    assert len(calls) == 2 and client.scheduler.resources.peaks["upload"] == 1
    assert client.library.completed(21, "dub", 1, 123, 456)
    assert client.library.completed(22, "dub", 1, 123, 456)


@pytest.mark.asyncio
async def test_getlist_waits_for_upload_and_does_not_reset_shared_gate(
    tmp_path, monkeypatch
):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    ctx.send = AsyncMock()
    resources = client.scheduler.resources
    async with resources.lease("upload", None):
        queued = asyncio.create_task(client.get_command("getlist").callback(ctx))
        await asyncio.sleep(0)
        client.scheduler.configure()
        assert client.scheduler.resources is resources and client.busy()
        ctx.send.assert_not_awaited()
    await asyncio.wait_for(queued, 2)
    assert list(ctx.send.await_args.kwargs) == ["file"]
    assert not resources.occupied["upload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["uploading", "needs_review"])
@pytest.mark.parametrize("previous_message", [None, "888"])
async def test_resuming_unknown_send_keeps_pending_identity_before_cache_check(
    tmp_path, monkeypatch, status, previous_message
):
    import animax.bot as app

    client, ctx, target, *_ = build(tmp_path, monkeypatch)
    if previous_message:
        client.library.record(
            21, "One Piece", "dub", 1, 123, 456, "uploaded", message_id=previous_message
        )
    client.library.record(
        21,
        "One Piece",
        "dub",
        1,
        123,
        456,
        status,
        needs_review=status == "needs_review",
    )
    cached = AsyncMock(return_value=None)
    download = AsyncMock()
    monkeypatch.setattr(app, "cached_video", cached)
    monkeypatch.setattr(app, "download", download)
    await client.run_job(
        ctx, target, Job(21, "dub", 1, 1, 456, worker_id=1), 10**9, False
    )
    assert cached.await_args.kwargs["pending_upload"] is True
    download.assert_not_awaited()
    row = client.library.data["anime"]["21"]["episodes"]["dub"]["1"]["deliveries"][
        "123:456"
    ]
    assert row["needs_review"] and row["status"] == "needs_review"
    if previous_message:
        assert row["messages"][0]["message_id"] == previous_message


def test_minimal_release_surface_and_entrypoint(tmp_path, monkeypatch):
    import runpy
    from pathlib import Path

    import animax.bootstrap as bootstrap
    import animax.bot as bot

    root = Path(__file__).resolve().parent.parent
    assert not any(
        (root / name).exists()
        for name in (
            "docs",
            "manage.py",
            "install.sh",
            "animax/service.py",
            "animax/doctor.py",
            "animax/migrate.py",
        )
    )
    readme = (root / "README.md").read_text()
    assert "pip install -r requirements.txt" in readme and "python bot.py" in readme
    assert "## Go Live streaming" in readme and "## Download and upload" in readme
    assert "does **not** replace `.download`" in readme
    calls = []
    monkeypatch.setattr(
        bootstrap, "ensure_runtime", lambda: calls.append("dependencies")
    )
    monkeypatch.setattr(bot, "main", lambda: calls.append("bot"))
    runpy.run_path(str(root / "bot.py"), run_name="__main__")
    assert calls == ["dependencies", "bot"]
