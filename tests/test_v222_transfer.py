import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

import animax.media as media
from animax.rate_limits import RateLimitError
from animax.resolver import DownloadSource
from animax.transfer_state import (
    discard_fragment_files,
    is_range_error,
    reset_source_download,
)


def test_cleanup_is_scoped_and_preserves_completed_fragments(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    keep = [
        "source.mp4.part",
        "source.mp4.ytdl",
        "episode.mp4",
        "ready.json",
        "anime.json",
        "settings.json",
        "process.log",
        "cookies.txt",
    ]
    for name in keep:
        (work / name).write_text("keep")
    for name in ["source.mp4.part-Frag72.part", "source.f123.mp4.part-Frag8"]:
        (work / name).write_text("discard")
    outside = tmp_path / "outside"
    outside.write_text("safe")
    (work / "source.mp4.part-Frag9.part").symlink_to(outside)
    directory = work / "source.directory"
    directory.mkdir()
    (directory / "keep").write_text("safe")
    assert discard_fragment_files(work) == 3
    assert all((work / name).read_text() == "keep" for name in keep)
    (work / "source-fingerprint.txt").write_text("old")
    reset_source_download(work)
    assert not (work / "source.mp4.part").exists()
    assert not (work / "source.mp4.ytdl").exists()
    assert not (work / "source-fingerprint.txt").exists()
    assert all((work / name).read_text() == "keep" for name in keep[2:])
    assert outside.read_text() == (directory / "keep").read_text() == "safe"


def test_range_detection_does_not_match_episode_numbers():
    assert is_range_error("ERROR: HTTP Error 416: Range Not Satisfiable")
    assert is_range_error("HTTP 416")
    assert not is_range_error("episode 416: fragment 72 not found")
    assert not is_range_error("HTTP Error 403: Forbidden")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "403"])
async def test_unrelated_failure_does_not_reset_download(
    tmp_path, monkeypatch, failure
):
    (tmp_path / "source.mp4.part").write_bytes(b"completed fragments")
    problem = (
        asyncio.CancelledError()
        if failure == "cancel"
        else media.MediaProcessError("yt-dlp", 1, "HTTP Error 403: Forbidden")
    )
    runner = AsyncMock(side_effect=problem)
    monkeypatch.setattr(media, "run_process", runner)
    with pytest.raises(type(problem)):
        await media.download(
            DownloadSource("https://unused.test/index.m3u8", {}, [], [], None),
            tmp_path,
            "sub",
        )
    runner.assert_awaited_once()
    assert (tmp_path / "source.mp4.part").read_bytes() == b"completed fragments"
    assert not (tmp_path / "cookies.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["interrupted", "transient416", "persistent416", "416then429"]
)
async def test_real_hls_resume_and_bounded_recovery(tmp_path, scenario):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    await media.run_process(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "2",
            "-g",
            "24",
            "-sc_threshold",
            "0",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            str(fixture / "index.m3u8"),
        ],
        fixture,
        timeout=30,
    )
    requests = []

    async def serve(request):
        name = request.match_info["name"]
        if name.endswith(".ts"):
            requests.append((name, request.headers.get("Range")))
            count = len(requests)
            if scenario == "persistent416" or (
                scenario in {"transient416", "416then429"} and count <= 2
            ):
                return web.Response(status=416, text="Range Not Satisfiable")
            if scenario == "416then429":
                return web.Response(
                    status=429, headers={"Retry-After": "123"}, text="Too Many Requests"
                )
            if scenario == "interrupted" and request.headers.get("Range"):
                return web.Response(status=416, text="No stale range allowed")
        return web.FileResponse(fixture / name)

    app = web.Application()
    app.router.add_get("/{name}", serve)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    work = tmp_path / "work"
    work.mkdir()
    if scenario == "interrupted":
        (work / "source.mp4.part").write_bytes((fixture / "index0.ts").read_bytes())
        (work / "source.mp4.ytdl").write_text(
            json.dumps({"downloader": {"current_fragment": {"index": 1}}})
        )
        (work / "source.mp4.part-Frag2.part").write_bytes(
            b"bad interrupted fragment" * 1000
        )
    phases = []
    try:
        stream = DownloadSource(
            f"http://127.0.0.1:{runner.addresses[0][1]}/index.m3u8", {}, [], [], None
        )
        operation = media.download(
            stream,
            work,
            "sub",
            quality=288,
            subtitle_mode="none",
            request_interval=0,
            timeout=30,
            on_phase=phases.append,
        )
        if scenario == "persistent416":
            with pytest.raises(media.MediaProcessError, match="416"):
                await operation
            assert len(requests) == 4
            assert "Enhancing" not in phases
            assert not (work / "episode.mp4").exists()
            assert not list(work.glob("source.*"))
        elif scenario == "416then429":
            with pytest.raises(RateLimitError) as caught:
                await operation
            assert caught.value.retry_after == 123
            assert len(requests) == 3
            assert "Enhancing" not in phases
        else:
            output, _ = await operation
            info = await media.probe(output)
            assert abs(float(info["format"]["duration"]) - 3) < 0.1
            assert "mp4" in info["format"]["format_name"]
            if scenario == "interrupted":
                assert [name for name, _ in requests] == ["index1.ts", "index2.ts"]
                assert all(header is None for _, header in requests)
            else:
                assert [name for name, _ in requests] == ["index0.ts"] * 3 + [
                    "index1.ts",
                    "index2.ts",
                ]
                assert "ANIMAX RECOVERY: HTTP 416" in (work / "process.log").read_text()
            assert "Enhancing" in phases
        assert not (work / "cookies.txt").exists()
    finally:
        await runner.cleanup()
