import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_debug_release import build
from test_v22_media import ffmpeg, local_source

import animax.bot as app
import animax.media as media
from animax.job import Job
from animax.resolver import DownloadSource
from animax.settings import Settings


def test_v23_defaults_and_done_text():
    cfg = Settings()
    assert (cfg.quality, cfg.encoder_preset, cfg.video_crf, cfg.encode_threads) == (
        1080,
        "veryfast",
        19,
        4,
    )
    assert (cfg.frame_rate, cfg.compression_mode, cfg.target_mb, cfg.auto_compress) == (
        "source",
        "quality",
        450,
        False,
    )
    job = Job(
        21,
        "dub",
        3,
        3,
        456,
        title="One Piece",
        current=3,
        phase="Done",
        output_size_bytes=398_100_000,
    )
    assert job.episode_text() == "**One Piece** | `E3` | Done | `398.1 MB`"
    job.output_size_bytes = None
    assert "size unavailable" in job.episode_text() and "100%" not in job.episode_text()


async def sparse_download(stream, work, language, **kwargs):
    size = 398_100_000 if work.name.endswith("-1") else 477_600_000
    source = work / "whole.mp4"
    with source.open("wb") as file:
        file.truncate(size)
    (work / "ready.json").write_text(
        json.dumps({"file": source.name, "language": language})
    )
    return source, None


@pytest.mark.asyncio
async def test_done_uses_each_committed_file_size_and_recovers_after_restart(
    tmp_path, monkeypatch
):
    client, ctx, target, messages, _ = build(tmp_path, monkeypatch)
    client.cfg.channel_id = 456
    monkeypatch.setattr(app, "download", sparse_download)
    original_send = target.send.side_effect

    async def post(*args, **kwargs):
        assert "Done" not in messages[-1].contents[-1]
        return await original_send(*args, **kwargs)

    target.send.side_effect = post
    job = Job(21, "dub", 1, 2, 456)
    await client.run_job(ctx, target, job, 1_000_000_000, False)
    assert job.sent == 2 and job.failed == 0
    assert messages[0].contents[-1] == "**One Piece** | `E1` | Done | `398.1 MB`"
    assert messages[1].contents[-1] == "**One Piece** | `E2` | Done | `477.6 MB`"
    assert all(not message.deleted for message in messages)
    recovered = Job(21, "dub", 1, 2, 456, current=1, title="One Piece")
    row = client.delivery(recovered, target)
    assert row["status"] == "uploaded" and row["size_bytes"] == 398_100_000
    row["progress_retained"] = False
    client.channel_for = AsyncMock(
        return_value=SimpleNamespace(fetch_message=AsyncMock(return_value=messages[0]))
    )
    await client.clear_progress(recovered, target, None, ctx)
    assert messages[0].contents[-1].endswith("Done | `398.1 MB`")
    assert recovered.output_size_bytes == 398_100_000
    assert target.send.await_count == 2


@pytest.mark.asyncio
async def test_soft_size_preference_never_compresses_a_600mb_file(
    tmp_path, monkeypatch, caplog
):
    client, ctx, target, messages, _ = build(tmp_path, monkeypatch)
    client.cfg.channel_id = 456
    client.cfg.auto_compress = True

    async def large_download(stream, work, language, **kwargs):
        source, note = await sparse_download(stream, work, language, **kwargs)
        with source.open("r+b") as file:
            file.truncate(600_000_000)
        return source, note

    fitter = AsyncMock()
    monkeypatch.setattr(app, "download", large_download)
    monkeypatch.setattr(app, "fit_whole_video", fitter)
    with caplog.at_level("INFO", logger="animax"):
        job = Job(21, "dub", 1, 1, 456)
        await client.run_job(ctx, target, job, 1_000_000_000, False)
    assert job.sent == 1 and job.failed == 0
    fitter.assert_not_awaited()
    assert "preferred_mb=400-500 relation=above policy=advisory" in caplog.text
    assert messages[0].contents[-1].endswith("Done | `600.0 MB`")


@pytest.mark.asyncio
async def test_preferred_size_setting_explains_it_is_not_a_bitrate_cap(
    tmp_path, monkeypatch
):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    ctx.send = AsyncMock()
    await client.get_command("settings").callback(ctx, "target_size", value="450")
    text = ctx.send.await_args.args[0]
    assert "400-500 MB" in text and "does not constrain bitrate" in text


@pytest.mark.asyncio
async def test_native_allocator_spends_more_on_motion_than_still_frames(
    tmp_path, monkeypatch
):
    reference = tmp_path / "mixed.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=gray:size=1440x1080:rate=24:duration=4",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1440x1080:rate=24:duration=4",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-threads",
        "4",
        "-qp",
        "0",
        reference,
    )
    encodes = local_source(monkeypatch, reference)
    source = DownloadSource("https://unused.test/video.m3u8", {}, [], [], None)
    output, _ = await media.download(
        source, tmp_path / "work", "sub", subtitle_mode="none", timeout=120
    )
    args = encodes[0]
    assert args[args.index("-crf") + 1] == "0"
    assert args[args.index("-preset") + 1] == "veryfast"
    assert "-profile:v" not in args
    assert not any(option in args for option in ("-maxrate", "-bufsize", "-b:v", "-r"))
    info = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_packets",
                "-show_entries",
                "packet=pts_time,size",
                "-of",
                "json",
                str(output),
            ]
        )
    )
    still = sum(int(p["size"]) for p in info["packets"] if float(p["pts_time"]) < 4)
    motion = sum(int(p["size"]) for p in info["packets"] if float(p["pts_time"]) >= 4)
    assert motion > 10 * still
    metrics = {
        "scope": "4s flat gray + 4s synthetic motion; not an anime episode",
        "still_packet_bytes": still,
        "motion_packet_bytes": motion,
        "output_bytes": output.stat().st_size,
    }
    (tmp_path / "allocation.json").write_text(json.dumps(metrics, indent=2))
    cached = await media.cached_video(
        tmp_path / "work", "sub", 1080, "none", target_mb=500
    )
    assert cached and cached[0] == output
