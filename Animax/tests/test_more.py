from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from animax.media import cached_video, download, fit_whole_video, probe, run_process
from animax.rate_limits import Cooldown, cooldown_seconds, parse_retry_after
from animax.resolver import Stream


def test_adaptive_pacing_and_retry_after(tmp_path):
    clock = Cooldown(tmp_path / "cdn_cooldown.json")
    clock.slow_down(0.25)
    assert clock.interval == 2
    clock.slow_down(0.25)
    assert clock.interval == 4
    clock.relax(0.25)
    assert clock.interval == 3.2
    assert Cooldown(tmp_path / "cdn_cooldown.json").interval == 3.2
    assert parse_retry_after("120") == 120
    assert parse_retry_after("nan") is None
    assert cooldown_seconds(1, 300, 900) == 900


@pytest.mark.asyncio
async def test_real_hls_progress_cache_and_whole_compression(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    source = fixture / "input.mp4"
    await run_process(
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
            "testsrc2=size=640x360:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-b:v",
            "1600k",
            "-g",
            "24",
            "-c:a",
            "aac",
            str(source),
        ],
        fixture,
        timeout=60,
    )
    await run_process(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-c",
            "copy",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            str(fixture / "index.m3u8"),
        ],
        fixture,
        timeout=60,
    )
    app = web.Application()
    app.router.add_static("/", fixture)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    stream = Stream(
        f"http://127.0.0.1:{port}/index.m3u8",
        {},
        [],
        SimpleNamespace(cookies=AsyncMock(return_value=[])),
    )
    updates = []
    phases = []
    try:
        output, note = await download(
            stream,
            tmp_path / "work",
            "dub",
            request_interval=0,
            on_progress=updates.append,
            on_phase=phases.append,
            timeout=60,
        )
        assert updates and updates[-1]["fraction"] == 1
        assert updates[-1]["downloaded"] > 0
        assert "Enhancing" in phases
        assert (await cached_video(tmp_path / "work", "dub", 1080, "burn"))[0] == output
        assert await cached_video(tmp_path / "work", "dub", 480, "burn") is None
        assert output.stat().st_size > 450000
        compressed = await fit_whole_video(output, 450000, timeout=60)
        assert compressed.stat().st_size <= 450000
        assert abs(float((await probe(compressed))["format"]["duration"]) - 6) < 0.2
        assert "mp4" in (await probe(compressed))["format"]["format_name"]
    finally:
        await runner.cleanup()
