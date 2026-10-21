from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from animax.media import download, probe, run_process
from animax.resolver import Stream


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["burn", "mux", "none"])
async def test_download_and_subtitles_with_local_synthetic_video(tmp_path, mode):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    video = fixture / "input.mp4"
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
            "testsrc2=size=320x240:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(video),
        ],
        fixture,
        timeout=30,
    )
    # Test the HLS downloader against locally generated, non-copyrighted media.
    await run_process(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-c",
            "copy",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            str(fixture / "index.m3u8"),
        ],
        fixture,
        timeout=30,
    )
    app = web.Application()
    app.router.add_static("/", fixture)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    async def body():
        return b"WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nSynthetic test caption\n"

    request = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(ok=True, status=200, body=body))
    )
    context = SimpleNamespace(cookies=AsyncMock(return_value=[]), request=request)
    stream = Stream(
        f"http://127.0.0.1:{port}/index.m3u8",
        {},
        [
            {
                "kind": "captions",
                "label": "English",
                "file": "https://example.com/sub.vtt",
            }
        ],
        context,
    )
    try:
        output, note = await download(
            stream,
            tmp_path / "work",
            "sub",
            subtitle_mode=mode,
            timeout=60,
            request_interval=0,
        )
        info = await probe(output)
        assert output.exists() and note is None
        assert "mp4" in info["format"]["format_name"].split(",")
        video_stream = next(
            item for item in info["streams"] if item["codec_type"] == "video"
        )
        assert (video_stream["width"], video_stream["height"]) == (1920, 1080)
        assert video_stream["r_frame_rate"] == "24/1"
        assert (
            video_stream["codec_name"] == "h264"
            and video_stream["pix_fmt"] == "yuv420p"
        )
        if mode != "none":
            assert (tmp_path / "work" / "subtitles.vtt").read_bytes() == await body()
        assert all(stream["codec_type"] != "data" for stream in info["streams"])
        assert float(info["format"]["duration"]) >= 1.9
        assert not (tmp_path / "work" / "cookies.txt").exists()
        if mode == "mux":
            assert any(s["codec_type"] == "subtitle" for s in info["streams"])
        if mode == "burn":
            assert output.name == "episode.mp4"
            assert not any(s["codec_type"] == "subtitle" for s in info["streams"])
    finally:
        await runner.cleanup()
