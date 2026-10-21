import json
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

import animax.media as media
from animax.encoding import video_filter
from animax.resolver import DownloadSource


def ffmpeg(*args):
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *map(str, args),
        ],
        check=True,
        timeout=120,
    )


def local_source(monkeypatch, reference):
    original = media.run_process
    encodes = []

    async def run(args, cwd, **kwargs):
        if len(args) > 1 and Path(args[1]).name == "downloader.py":
            shutil.copyfile(reference, cwd / "source.mp4")
            return ""
        encodes.append(args)
        return await original(args, cwd, **kwargs)

    monkeypatch.setattr(media, "run_process", run)
    return encodes


@pytest.mark.asyncio
@pytest.mark.parametrize("fps", ["24000/1001", "24", "30000/1001", "30", "60"])
async def test_native_frame_rate_is_preserved(tmp_path, monkeypatch, fps):
    reference = tmp_path / "input.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=640x360:rate={fps}",
        "-frames:v",
        "48",
        "-c:v",
        "libx264",
        "-threads",
        "2",
        "-crf",
        "18",
        reference,
    )
    encodes = local_source(monkeypatch, reference)
    source = DownloadSource("https://unused.test/video.m3u8", {}, [], [], None)
    output, _ = await media.download(
        source, tmp_path / "work", "sub", subtitle_mode="none", timeout=120
    )
    info = await media.probe(output)
    video = next(item for item in info["streams"] if item["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1920, 1080)
    assert Fraction(video["avg_frame_rate"]) == Fraction(fps)
    assert video["codec_name"] == "h264"
    assert (
        len(encodes) == 1
        and "-maxrate" not in encodes[0]
        and "-bufsize" not in encodes[0]
    )
    assert encodes[0][encodes[0].index("-preset") + 1] == "veryfast"
    assert encodes[0][encodes[0].index("-x264-params") + 1] == "sync-lookahead=0"
    assert "-r" not in encodes[0]
    assert "fps=" not in encodes[0][encodes[0].index("-vf") + 1]
    ready = json.loads((tmp_path / "work/ready.json").read_text())
    assert ready["fps"] == "source" and Fraction(ready["source_fps"]) == Fraction(fps)
    assert ready["compression_mode"] == "quality"


def ssim(encoded, reference):
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-threads",
            "2",
            "-i",
            str(encoded),
            "-threads",
            "2",
            "-i",
            str(reference),
            "-filter_complex_threads",
            "1",
            "-lavfi",
            "[1:v]pad=1920:1080:(ow-iw)/2:(oh-ih)/2[ref];[0:v][ref]ssim",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return float(re.findall(r"All:([0-9.]+)", result.stderr)[-1])


@pytest.mark.asyncio
async def test_action_profile_improves_over_tight_ultrafast_budget(
    tmp_path, monkeypatch
):
    reference = tmp_path / "motion.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1440x1080:rate=24",
        "-frames:v",
        "72",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-threads",
        "2",
        "-qp",
        "0",
        reference,
    )
    local_source(monkeypatch, reference)
    output, _ = await media.download(
        DownloadSource("https://unused.test/video.m3u8", {}, [], [], None),
        tmp_path / "work",
        "sub",
        subtitle_mode="none",
        timeout=120,
    )
    old = tmp_path / "old-ultrafast.mp4"
    filters = video_filter({"width": 1440, "height": 1080}, 1080)
    ffmpeg(
        "-threads",
        "2",
        "-i",
        reference,
        "-filter_threads",
        "1",
        "-vf",
        filters,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-threads",
        "4",
        "-crf",
        "23",
        "-maxrate",
        "1960000",
        "-bufsize",
        "3920000",
        "-pix_fmt",
        "yuv420p",
        old,
    )
    old_score = ssim(old, reference)
    new_score = ssim(output, reference)
    result = {
        "old_ssim": old_score,
        "new_ssim": new_score,
        "old_bytes": old.stat().st_size,
        "new_bytes": output.stat().st_size,
        "scope": "3-second synthetic moving-pattern fixture, not a representative anime quality guarantee",
    }
    (tmp_path / "action-quality.json").write_text(json.dumps(result, indent=2))
    assert new_score > old_score + 0.005


@pytest.mark.asyncio
async def test_sub_caption_background_is_not_filled(tmp_path, monkeypatch):
    reference = tmp_path / "white.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=white:size=384x288:rate=24",
        "-frames:v",
        "24",
        "-c:v",
        "libx264",
        reference,
    )
    local_source(monkeypatch, reference)
    track = {
        "kind": "captions",
        "label": "English",
        "file": "https://unused.test/cc.vtt",
    }
    caption = b"WEBVTT\n\n00:00.000 --> 00:01.000\nWWWWWWWW\n"
    images = {}
    for language in ("sub", "dub"):
        source = DownloadSource(
            "https://unused.test/video.m3u8", {}, [track], [], caption
        )
        output, _ = await media.download(
            source, tmp_path / language, language, quality=288, timeout=30
        )
        images[language] = subprocess.check_output(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ]
        )

    def dark_pixels(rgb):
        return sum(
            max(rgb[(y * 512 + x) * 3 : (y * 512 + x) * 3 + 3]) < 25
            for y in range(225, 265)
            for x in range(180, 335)
        )

    assert dark_pixels(images["dub"]) > dark_pixels(images["sub"]) + 100


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["ultrafast", "superfast", "veryfast", "fast"])
async def test_selected_preset_has_no_forced_analysis_overrides(
    tmp_path, monkeypatch, caplog, preset
):
    reference = tmp_path / "input.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=24",
        "-frames:v",
        "12",
        "-c:v",
        "libx264",
        reference,
    )
    encodes = local_source(monkeypatch, reference)
    with caplog.at_level("INFO", logger="animax.media"):
        output, _ = await media.download(
            DownloadSource("https://unused.test/video.m3u8", {}, [], [], None),
            tmp_path / "work",
            "sub",
            quality=288,
            subtitle_mode="none",
            encoder_preset=preset,
            timeout=30,
        )
    args = encodes[0]
    assert args[args.index("-preset") + 1] == preset
    assert args[args.index("-x264-params") + 1] == "sync-lookahead=0"
    assert "-maxrate" not in args and "-bufsize" not in args
    assert "ENCODE_START" in caplog.text and "ENCODE_DONE seconds=" in caplog.text
    assert f"preset={preset}" in caplog.text
    assert output.is_file()
