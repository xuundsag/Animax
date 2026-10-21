import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_debug_release import build

import animax.bot as app
import animax.media as media
from animax.bot import Job
from animax.rate_limits import RateLimitError
from animax.resolver import (
    LanguageUnavailable,
    ResolveError,
    resolve_with_fallback,
    unavailable_payload,
)
from animax.search import find_uploads
from animax.storage import Library


@pytest.mark.asyncio
async def test_retained_status_chain_and_download_url(tmp_path, monkeypatch):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    client.cfg.channel_id = 456
    original = target.send.side_effect
    accepted = []

    async def post(*args, **kwargs):
        sent = await original(*args, **kwargs)

        async def edit(**values):
            assert client.library.completed(21, "dub", sent.id - 2000, 123, 456)
            accepted.append(values["content"])

        sent.edit = AsyncMock(side_effect=edit)
        return sent

    target.send.side_effect = post
    job = Job(21, "dub", 1, 3, 456)
    await client.run_job(ctx, target, job, 10**9, False)
    assert job.sent == 3 and job.failed == 0
    assert all(
        not m.deleted and "| Done | `0.0 MB`" in m.contents[-1] for m in messages
    )
    calls = target.send.await_args_list
    assert "reference" not in calls[0].kwargs
    for ep, call in enumerate(calls[1:], 2):
        reference = call.kwargs["reference"]
        assert (
            reference.message_id == 2000 + ep - 1
            and reference.fail_if_not_exists is False
        )
        assert call.kwargs["mention_author"] is False
    assert accepted == []
    assert calls[0].args[0] == "**One Piece** | `E1`"
    row = client.library.data["anime"]["21"]["episodes"]["dub"]["2"]["deliveries"][
        "123:456"
    ]
    assert row["progress_retained"] and row["reply_to_message_id"] == "2001"
    assert row["caption_update_pending"] is False and not row.get("download_url")


@pytest.mark.asyncio
async def test_dub_fallback_actual_language_and_next_episode_reset(
    tmp_path, monkeypatch
):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    seen = []

    class Resolver:
        def __init__(self, *a):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        @asynccontextmanager
        async def resolve(self, mal, language, episode):
            seen.append((episode, language))
            if episode == 1 and language == "dub":
                raise LanguageUnavailable("Dub episode not found")
            stream = SimpleNamespace(episode=episode)
            stream.detach = AsyncMock(return_value=stream)
            yield stream

    monkeypatch.setattr(app, "Resolver", Resolver)
    job = Job(21, "dub", 1, 2, 456)
    await client.run_job(ctx, target, job, 10**9, False)
    assert seen == [(1, "dub"), (1, "sub"), (2, "dub")]
    assert job.language == "dub" and job.actual_language == "dub" and job.sent == 2
    episodes = client.library.data["anime"]["21"]["episodes"]["dub"]
    assert episodes["1"]["deliveries"]["123:456"]["actual_language"] == "sub"
    assert episodes["2"]["deliveries"]["123:456"]["actual_language"] == "dub"
    assert all(
        "SUB (dub unavailable)" not in call.args[0]
        for call in target.send.await_args_list
    )
    assert any(
        "Dub unavailable; falling back to sub without captions." in call.args[0]
        for call in ctx.send.await_args_list
    )
    assert target.upload_files.await_args_list[0].args[0].filename.endswith("_sub.mp4")
    await client.run_job(ctx, target, Job(21, "dub", 1, 2, 456), 10**9, False)
    assert target.send.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ResolveError("Timeout"),
        RateLimitError(300),
        RuntimeError("HTTP 403"),
        RuntimeError("HTTP 500"),
    ],
)
async def test_no_false_language_fallback(error):
    calls = []

    class Resolver:
        @asynccontextmanager
        async def resolve(self, mal, language, episode):
            calls.append(language)
            raise error
            yield

    with pytest.raises(type(error)):
        async with resolve_with_fallback(Resolver(), 21, "dub", 1):
            pass
    assert calls == ["dub"]


@pytest.mark.asyncio
async def test_failure_after_resolution_does_not_fallback():
    calls = []

    class Resolver:
        @asynccontextmanager
        async def resolve(self, mal, language, episode):
            calls.append(language)
            yield object()

    with pytest.raises(LanguageUnavailable):
        async with resolve_with_fallback(Resolver(), 21, "dub", 1):
            raise LanguageUnavailable("A downstream failure cannot trigger fallback")
    assert calls == ["dub"]


def test_payload_availability_evidence():
    assert unavailable_payload({"sources": []})
    assert unavailable_payload({"message": "Episode not found"})
    assert not unavailable_payload({"sources": [], "enc": "secret"})
    assert not unavailable_payload({"sources": [], "message": "Rate limit exceeded"})
    assert not unavailable_payload({"sources": [], "status": 429})
    assert not unavailable_payload({"message": "Request timed out"})


def test_fuzzy_search_order_typos_filter_and_top_five(tmp_path):
    lib = Library(tmp_path / "anime.json")
    for ep in range(1, 9):
        lib.record(
            21,
            "One Piece",
            "dub",
            ep,
            123,
            456,
            "uploaded",
            message_id=2000 + ep,
            jump_url=f"https://discord.com/channels/1/456/{2000 + ep}",
        )
    lib.record(21, "One Piece", "dub", 1, 999, 456, "uploaded", message_id=999)
    for query in [
        "One piece episode 1",
        "eisode 1 onepimse",
        "1 episode piece one",
        "one piece E1",
    ]:
        results = find_uploads(lib.data, query, 123)
        assert (
            len(results) == 1
            and results[0]["episode"] == 1
            and results[0]["message_id"] == "2001"
        )
    assert len(find_uploads(lib.data, "One Piece", 123)) == 5
    assert not find_uploads(lib.data, "One Piece episode 999", 123)
    assert not find_uploads(lib.data, "zzzxqrrr", 123)


@pytest.mark.asyncio
async def test_help_and_getlist_attachment_only(tmp_path, monkeypatch):
    client, ctx, target, messages, events = build(tmp_path, monkeypatch)
    ctx.send = AsyncMock()
    await client.get_command("help").callback(ctx)
    text = ctx.send.await_args.args[0]
    assert text.startswith("**Commands**\n```")
    assert all(command.name in text for command in client.commands)
    await client.get_command("help").callback(ctx, "search")
    assert ".search QUERY" in ctx.send.await_args.args[0]
    received = []

    async def send(*args, **kwargs):
        assert not args and set(kwargs) == {"file"}
        assert kwargs["file"].filename == "anime.json"
        received.append(json.loads(kwargs["file"].fp.read()))

    ctx.send = AsyncMock(side_effect=send)
    await client.get_command("getlist").callback(ctx)
    assert received == [client.library.data]


@pytest.mark.asyncio
async def test_legacy_cache_is_not_v2(tmp_path):
    (tmp_path / "ready.json").write_text(
        json.dumps(
            {
                "file": "whole.mp4",
                "language": "dub",
                "quality": 720,
                "subtitle_mode": "burn",
            }
        )
    )
    assert await media.cached_video(tmp_path, "dub", 720, "burn") is None


@pytest.mark.parametrize(
    "width,height,sar,quality",
    [
        (384, 288, "1:1", 720),
        (720, 576, "16:15", 720),
        (640, 360, "1:1", 720),
        (720, 300, "1:1", 720),
    ],
)
def test_real_ffmpeg_full_frame_padding_and_anamorphic(
    tmp_path, width, height, sar, quality
):
    filter = media.video_filter(
        {"width": width, "height": height, "sample_aspect_ratio": sar}, quality
    )
    source = f"color=white:size={width}x{height}:rate=1,drawbox=x=0:y=0:w=8:h=ih:color=red:t=fill,drawbox=x=iw-8:y=0:w=8:h=ih:color=lime:t=fill,setsar={sar.replace(':', '/')}"
    result = tmp_path / "proof.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            source,
            "-t",
            "1",
            "-vf",
            filter,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(result),
        ],
        check=True,
    )
    info = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(result)]
        )
    )["streams"][0]
    w, h = info["width"], info["height"]
    assert w * 9 == h * 16 and info["sample_aspect_ratio"] == "1:1"
    rgb = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(result),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
    )
    import re

    sw, sh = map(int, re.search(r"scale=(\d+):(\d+)", filter).groups())
    x = (w - sw) // 2
    y = h // 2

    def pixel(x, y):
        return tuple(rgb[(y * w + x) * 3 : (y * w + x) * 3 + 3])

    assert (
        pixel(x + 3, y)[0] > 180 and pixel(x + sw - 4, y)[1] > 180
    )  # Both original frame edges survived.
    assert min(pixel(w // 2, h // 2)) > 220
    if x > 4:
        assert (
            max(pixel(x // 2, y)) < 12
        )  # Solid baked-in side padding, not player CSS.
    if (h - sh) // 2 > 4:
        assert max(pixel(w // 2, 1)) < 12


@pytest.mark.asyncio
async def test_real_dub_opaque_burn_single_encode(tmp_path, monkeypatch):
    from animax.resolver import Stream

    work = tmp_path / "work"
    work.mkdir()
    original = media.run_process
    encoding = []

    async def run(args, cwd, **kw):
        if len(args) > 1 and Path(args[1]).name == "downloader.py":
            await original(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=white:size=384x288:rate=5",
                    "-t",
                    "2",
                    "-c:v",
                    "libx264",
                    str(work / "source.mp4"),
                ],
                work,
                timeout=30,
            )
            return ""
        if "-c:v" in args and "libx264" in args:
            encoding.append(args)
        return await original(args, cwd, **kw)

    monkeypatch.setattr(media, "run_process", run)
    track = {
        "kind": "captions",
        "label": "English",
        "file": "https://example.invalid/cc.vtt",
    }
    stream = Stream(
        "https://example.invalid/index.m3u8",
        {},
        [track],
        SimpleNamespace(cookies=AsyncMock(return_value=[])),
    )
    stream.caption_bytes = AsyncMock(
        return_value=b"WEBVTT\n\n00:00.000 --> 00:02.000\nOpaque Caption\n"
    )
    result, note = await media.download(
        stream, work, "dub", subtitle_mode="none", quality=288, timeout=30
    )
    assert note is None and len(encoding) == 1
    filters = encoding[0][encoding[0].index("-vf") + 1]
    assert (
        "pad=" in filters
        and "BorderStyle=3" in filters
        and "OutlineColour=&H00000000" in filters
    )
    rgb = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(result),
            "-ss",
            "0.8",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
    )
    w = 512
    # Black box over the WHITE central picture, not just the outside black bars.
    black = sum(
        max(rgb[(y * w + x) * 3 : (y * w + x) * 3 + 3]) < 15
        for y in range(200, 270)
        for x in range(80, 432)
    )
    assert black > 100
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(result),
            "-ss",
            "0.8",
            "-frames:v",
            "1",
            str(tmp_path / "caption-proof.png"),
        ],
        check=True,
    )


def test_http200_explicit_missing_source_page_only():
    from animax.resolver import unavailable_document

    assert unavailable_document(
        '<div class="error-code">Error Code: <span>410</span></div>'
    )
    assert unavailable_document(
        '<div class="error-code">Error Code: <span>404</span></div>'
    )
    assert not unavailable_document(
        '<div class="error-code">Error Code: <span>429</span></div>'
    )
    assert not unavailable_document("<h1>Connection timed out</h1>")
    assert not unavailable_document(
        '<script>var message="Video not found";</script><div>player</div>'
    )
