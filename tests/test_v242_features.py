import json
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_debug_release import build
from test_v22_media import ffmpeg, local_source

import animax.bot as app
import animax.media as media
from animax.commands import parse_download_tail
from animax.encoding import video_filter
from animax.job import Job
from animax.media import PROCESSING_VERSION
from animax.resolver import (
    CaptionUnavailable,
    DownloadSource,
    LanguageUnavailable,
    resolve_with_fallback,
)
from animax.settings import SAFE_OUTPUT_MB, Settings, read_overrides, save_setting


@pytest.mark.parametrize(
    "tail,expected",
    [
        ("", ("resume", "")),
        ("force", ("force", "")),
        ("<#123>", ("resume", "<#123>")),
        ("resume <#123>", ("resume", "<#123>")),
        ("force <#123>", ("force", "<#123>")),
    ],
)
def test_download_tail_requires_channel_last(tail, expected):
    assert parse_download_tail(tail) == expected


@pytest.mark.parametrize("tail", ["<#123> force", "resume extra <#123>", "bad extra"])
def test_download_tail_rejects_misordered_or_extra_arguments(tail):
    from discord.ext import commands

    with pytest.raises(commands.BadArgument):
        parse_download_tail(tail)


def test_new_compression_defaults_and_old_one_gib_setting_migrate_to_950(
    tmp_path, monkeypatch
):
    cfg = Settings()
    assert not cfg.compress_all and not cfg.lossless
    assert (
        cfg.caption_font == "liberation" and cfg.output_max_mb == SAFE_OUTPUT_MB == 950
    )
    monkeypatch.setenv("MAX_OUTPUT_MB", "1000")
    assert Settings.from_env().output_max_mb == 950
    path = tmp_path / "settings.json"
    path.write_text('{"max_size": "1000", "reencode": "on"}')
    assert read_overrides(path) == {"max_size": 950}
    updated = save_setting(path, cfg, "compressall", "on")
    updated = save_setting(path, updated, "lossless", "on")
    updated = save_setting(path, updated, "caption_font", "dejavu-serif")
    assert updated.compress_all and updated.lossless
    assert updated.caption_font == "dejavu-serif"
    stored = json.loads(path.read_text())
    assert stored["compressall"] == "on"
    assert stored["lossless"] == "on"
    assert stored["caption_font"] == "dejavu-serif"


@pytest.mark.asyncio
async def test_settings_command_exposes_font_and_lossless_options(
    tmp_path, monkeypatch
):
    client, ctx, *_ = build(tmp_path, monkeypatch)
    ctx.send = AsyncMock()
    await client.get_command("settings").callback(ctx, "caption_font", value="dejavu")
    assert client.cfg.caption_font == "dejavu"
    await client.get_command("settings").callback(ctx, "lossless", value="on")
    assert client.cfg.lossless is True
    await client.get_command("settings").callback(ctx, "compressall", value="true")
    assert client.cfg.compress_all is True
    await client.get_command("settings").callback(ctx, "compressall", value="false")
    assert client.cfg.compress_all is False
    await client.get_command("settings").callback(ctx, "caption_font", value="comic")
    assert (
        "liberation, dejavu, dejavu-serif, dejavu-mono" in ctx.send.await_args.args[0]
    )


def test_caption_font_is_a_safe_selectable_libass_family():
    video = {"width": 1440, "height": 1080, "sample_aspect_ratio": "1:1"}
    expected = {
        "liberation": "Liberation Sans",
        "dejavu": "DejaVu Sans",
        "dejavu-serif": "DejaVu Serif",
        "dejavu-mono": "DejaVu Sans Mono",
    }
    for selected, family in expected.items():
        filt = video_filter(video, 1080, True, language="dub", caption_font=selected)
        assert f"FontName={family}" in filt
        assert "BorderStyle=3" in filt and "BackColour=&H00000000" in filt
    with pytest.raises(ValueError, match="CAPTION_FONT"):
        video_filter(video, 1080, True, caption_font="arbitrary-filter-text")


@pytest.mark.parametrize(
    "options,expected_crf,expected_mode",
    [
        ({}, "0", "preserve"),
        ({"compress_all": True}, "19", "quality"),
        ({"upload_limit": 41_000}, "19", "safe"),
        ({"compress_all": True, "lossless": True}, "0", "lossless"),
    ],
)
@pytest.mark.asyncio
async def test_crf_policy_and_lossless_override(
    tmp_path, monkeypatch, options, expected_crf, expected_mode
):
    reference = tmp_path / "input.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=24",
        "-t",
        "1.0",
        "-c:v",
        "libx264",
        "-threads",
        "2",
        "-crf",
        "18",
        reference,
    )
    commands = local_source(monkeypatch, reference)
    output, _ = await media.download(
        DownloadSource("https://unused.test/video.mp4", {}, [], [], None),
        tmp_path
        / f"work-{len(options)}-{expected_crf}-{options.get('upload_limit', 0)}",
        "sub",
        subtitle_mode="none",
        timeout=120,
        **options,
    )
    args = commands[0]
    assert args[args.index("-crf") + 1] == expected_crf
    if expected_crf == "0":
        assert "-profile:v" not in args and "-maxrate" not in args
    ready = json.loads((output.parent / "ready.json").read_text())
    assert ready["processing_version"] == PROCESSING_VERSION == 6
    assert ready["effective_crf"] == int(expected_crf)
    assert ready["effective_mode"] == expected_mode
    assert output.exists()


@pytest.mark.asyncio
async def test_dub_without_caption_track_falls_back_once_to_uncaptioned_sub(
    tmp_path, monkeypatch
):
    client, ctx, target, _, _ = build(tmp_path, monkeypatch)
    resolutions = []
    detaches = []
    downloads = []

    class Resolver:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @asynccontextmanager
        async def resolve(self, mal, language, episode):
            resolutions.append((episode, language))
            stream = NS(tracks=[])

            async def detach(actual_language, with_captions=True):
                detaches.append((episode, actual_language, with_captions))
                return NS(tracks=stream.tracks)

            stream.detach = detach
            yield stream

    async def fake_download(stream, work, language, **kwargs):
        downloads.append((language, kwargs["subtitle_mode"]))
        path = work / "prepared.mp4"
        path.write_bytes(b"whole test episode")
        (work / "ready.json").write_text(
            json.dumps({"file": path.name, "language": language})
        )
        return path, None

    monkeypatch.setattr(app, "Resolver", Resolver)
    monkeypatch.setattr(app, "download", fake_download)
    job = Job(21, "dub", 1, 2, 456)
    await client.run_job(ctx, target, job, 1_000_000_000, False)

    assert resolutions == [(1, "dub"), (1, "sub"), (2, "dub"), (2, "sub")]
    assert detaches == [(1, "sub", False), (2, "sub", False)]
    assert downloads == [("sub", "none"), ("sub", "none")]
    notices = [
        call.args[0]
        for call in ctx.send.await_args_list
        if "falling back" in call.args[0]
    ]
    assert len(notices) == 1 and "Dub captions unavailable" in notices[0]
    assert all(
        "SUB" not in call.args[0] and "Captions unavailable" not in call.args[0]
        for call in target.send.await_args_list
    )
    assert job.sent == 2 and job.fallback_notice_sent
    assert "SUB (dub unavailable)" not in job.episode_text()


@pytest.mark.asyncio
async def test_missing_dub_source_also_notifies_only_once(tmp_path, monkeypatch):
    client, ctx, target, *_ = build(tmp_path, monkeypatch)
    downloads = []

    class Resolver:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @asynccontextmanager
        async def resolve(self, mal, language, episode):
            if language == "dub":
                raise LanguageUnavailable("private provider text")
            stream = NS(tracks=[])
            stream.detach = AsyncMock(return_value=stream)
            yield stream

    async def fake_download(stream, work, language, **kwargs):
        downloads.append((language, kwargs["subtitle_mode"]))
        path = work / "prepared.mp4"
        path.write_bytes(b"whole test episode")
        (work / "ready.json").write_text(
            json.dumps({"file": path.name, "language": language})
        )
        return path, None

    monkeypatch.setattr(app, "Resolver", Resolver)
    monkeypatch.setattr(app, "download", fake_download)
    await client.run_job(ctx, target, Job(21, "dub", 1, 2, 456), 1_000_000_000, False)
    notices = [
        call.args[0]
        for call in ctx.send.await_args_list
        if "falling back" in call.args[0]
    ]
    assert len(notices) == 1 and "Dub unavailable" in notices[0]
    assert all(row == ("sub", "none") for row in downloads)
    assert all("private provider text" not in item for item in notices)


@pytest.mark.asyncio
async def test_dub_caption_track_with_missing_caption_bytes_falls_back_to_sub():
    track = {
        "kind": "captions",
        "file": "https://captions.example.test/episode.vtt",
        "label": "English Dub",
    }
    resolutions = []
    closed = []
    prepared = []

    class DubStream:
        tracks = [track]

        async def prepare_caption(self, language):
            prepared.append(language)
            raise CaptionUnavailable("Subtitle server returned HTTP 404.")

    class SubStream:
        tracks = []

    class Resolver:
        @asynccontextmanager
        async def resolve(self, mal, language, episode):
            resolutions.append(language)
            stream = DubStream() if language == "dub" else SubStream()
            try:
                yield stream
            finally:
                closed.append(language)

    async with resolve_with_fallback(Resolver(), 21, "dub", 1) as resolved:
        stream, actual, reason = resolved
        assert isinstance(stream, SubStream)
        assert actual == "sub"
        assert reason == "dub captions unavailable"

    assert resolutions == ["dub", "sub"]
    assert prepared == ["dub"]
    assert closed == ["dub", "sub"]


@pytest.mark.asyncio
async def test_stream_caption_prefetch_is_cached_for_detach():
    from animax.resolver import Stream

    track = {
        "kind": "captions",
        "file": "https://captions.example.test/episode.vtt",
        "label": "English Dub",
    }
    response = NS(status=200, ok=True, body=AsyncMock(return_value=b"WEBVTT\n"))
    request = NS(get=AsyncMock(return_value=response))
    context = NS(request=request, cookies=AsyncMock(return_value=[]))
    stream = Stream("https://video.example.test/episode.m3u8", {}, [track], context)

    assert await stream.prepare_caption("dub")
    detached = await stream.detach("dub")
    assert detached.captions == b"WEBVTT\n"
    request.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_marks_missing_caption_http_response_unavailable():
    from animax.resolver import Stream

    track = {
        "kind": "captions",
        "file": "https://captions.example.test/episode.vtt",
        "label": "English Dub",
    }
    request = NS(get=AsyncMock(return_value=NS(status=404, ok=False, headers={})))
    stream = Stream(
        "https://video.example.test/episode.m3u8", {}, [track], NS(request=request)
    )

    with pytest.raises(CaptionUnavailable):
        await stream.prepare_caption("dub")
