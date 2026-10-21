from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from animax.aniskip import normalize_intervals
from animax.encoding import video_filter
from animax.media import (
    PROCESSING_VERSION,
    cached_video,
    cut_filter_graph,
    keep_intervals,
    normalize_skip_intervals,
    probe,
    run_process,
)
from animax.titlecard import create_title_card


def test_aniskip_intervals_are_clipped_sorted_and_merged():
    result = normalize_intervals(
        {
            "results": [
                {"skipType": "ed", "interval": {"startTime": 1020, "endTime": 1080}},
                {
                    "skipType": "op",
                    "interval": {"startTime": 70, "endTime": 115},
                    "episodeLength": 900,
                },
                {
                    "skipType": "op",
                    "interval": {"startTime": 80, "endTime": 125},
                    "episodeLength": 1060,
                },
                {"skipType": "recap", "interval": {"startTime": 300, "endTime": 400}},
                {
                    "skipType": "mixed-op",
                    "interval": {"startTime": 120, "endTime": 160},
                },
            ]
        },
        1060,
    )
    assert result == [(80.0, 125.0), (1020.0, 1060.0)]


def test_cut_intervals_keep_content_between_op_and_ed():
    assert normalize_skip_intervals([(80, 120), (110, 160), (540, 600)], 600) == [
        (80.0, 160.0),
        (540.0, 600.0),
    ]
    assert keep_intervals(600, [(80, 160), (540, 600)]) == [(0.0, 80.0), (160.0, 540.0)]


def test_filter_graph_concats_matching_video_and_audio_parts():
    graph = cut_filter_graph(
        "scale=1920:1080,setsar=1,pad=1920:1080", [(0, 20), (60, 100)], True
    )
    assert "split=2" in graph
    assert "asplit=2" in graph
    assert "concat=n=2:v=1:a=1[outv][outa]" in graph
    assert "trim=start=60.000:end=100.000" in graph


def test_filter_graph_works_without_audio():
    graph = cut_filter_graph("scale=1920:1080", [(5, 12)], False)
    assert "[0:v:0]scale=1920:1080,trim=start=5.000:end=12.000" in graph
    assert "concat=n=1:v=1:a=0[outv]" in graph


@pytest.mark.asyncio
async def test_cache_marker_is_versioned_and_separates_skip_state(
    tmp_path, monkeypatch
):
    work = tmp_path / "cached"
    work.mkdir()
    video = work / "episode.mp4"
    video.write_bytes(b"test placeholder")
    marker = {
        "processing_version": PROCESSING_VERSION,
        "file": "episode.mp4",
        "language": "sub",
        "skip_intro_outro": True,
        "quality": 1080,
        "subtitle_mode": "burn",
        "encoder_preset": "ultrafast",
        "fps": "source",
        "target_mb": None,
        "video_crf": 19,
        "compression_mode": "quality",
        "compress_all": False,
        "lossless": False,
        "caption_font": "liberation",
        "safe_output_mb": 950,
    }
    (work / "ready.json").write_text(json.dumps(marker))

    async def valid_probe(_path):
        return {"format": {"duration": "60"}, "streams": [{"codec_type": "video"}]}

    monkeypatch.setattr("animax.media.probe", valid_probe)
    cached = await cached_video(work, "sub", 1080, "burn", encoder_preset="ultrafast")
    assert cached is None
    cached = await cached_video(
        work, "sub", 1080, "burn", encoder_preset="ultrafast", skip_intro_outro=True
    )
    assert cached == (video, None)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is not installed")
@pytest.mark.asyncio
async def test_ffmpeg_skip_filter_outputs_complete_1080p_episode(tmp_path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "episode.mp4"
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
            "testsrc2=size=640x360:rate=24:duration=8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=8",
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
    video = next(
        stream for stream in info["streams"] if stream["codec_type"] == "video"
    )
    (tmp_path / "subtitles.vtt").write_text(
        "\n".join(
            [
                "WEBVTT",
                "",
                "00:00:00.000 --> 00:00:08.000",
                "Caption font integration check",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "fonts").symlink_to(
        Path(__file__).resolve().parents[1] / "assets" / "fonts",
        target_is_directory=True,
    )
    filters = video_filter(
        video, 1080, True, frame_rate="30", caption_font="liberation"
    )
    graph = cut_filter_graph(filters, keep_intervals(8, [(1, 2), (6, 7)]), True)
    await run_process(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            graph,
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(output),
        ],
        tmp_path,
        timeout=120,
        max_bytes=256 * 1024**2,
    )
    result = await probe(output)
    output_video = next(
        stream for stream in result["streams"] if stream["codec_type"] == "video"
    )
    assert int(output_video["width"]) == 1920
    assert int(output_video["height"]) == 1080
    assert float(result["format"]["duration"]) == pytest.approx(6.0, abs=0.5)


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
