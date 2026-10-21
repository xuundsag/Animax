from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from animax.stream_media import (
    attachment_extension,
    ensure_playable,
    is_video_attachment,
    probe,
)
from animax.titlecard import create_title_card


def test_video_attachment_detection_uses_discord_mime_or_suffix():
    assert is_video_attachment("episode.MP4")
    assert is_video_attachment("upload.bin", "video/mp4; charset=binary")
    assert not is_video_attachment("cover.jpg", "image/jpeg")
    assert attachment_extension("upload.bin", "video/x-matroska") == ".mkv"
    assert attachment_extension("episode.webm", "application/octet-stream") == ".webm"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is not installed")
@pytest.mark.asyncio
async def test_ffprobe_and_frame_decode_accept_a_local_uploaded_video(tmp_path):
    source = tmp_path / "episode.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        timeout=60,
    )
    info = await probe(source)
    assert float(info["format"]["duration"]) == pytest.approx(3, abs=0.5)
    playable, validated = await ensure_playable(
        source,
        max_bytes=512 * 1024**2,
        timeout=90,
        disk_root=tmp_path,
    )
    assert playable == source
    assert any(stream["codec_type"] == "video" for stream in validated["streams"])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is not installed")
@pytest.mark.asyncio
async def test_title_card_is_1080p_and_has_silent_audio(tmp_path):
    font = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "fonts"
        / "LiberationSans-Regular.ttf"
    )
    card = await create_title_card(tmp_path / "cards", "One Piece", 7, font=str(font))
    info = await probe(card)
    video = next(
        stream for stream in info["streams"] if stream["codec_type"] == "video"
    )
    assert (int(video["width"]), int(video["height"])) == (1920, 1080)
    assert any(stream["codec_type"] == "audio" for stream in info["streams"])
    assert float(info["format"]["duration"]) == pytest.approx(2.8, abs=0.2)


def test_stream_media_has_no_intro_outro_processing_api():
    from animax import stream_media

    assert not hasattr(stream_media, "normalize_skip_intervals")
    assert not hasattr(stream_media, "cut_filter_graph")
    assert not hasattr(stream_media, "get_skip_intervals")
