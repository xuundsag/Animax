from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from .aniskip import get_skip_intervals
from .diagnostics import error_hint, error_summary
from .disk import disk_usage
from .encoding import video_filter
from .rate_limits import RateLimitError
from .resolver import Stream, choose_caption
from .settings import SAFE_OUTPUT_MB
from .transfer_state import (
    discard_fragment_files,
    is_range_error,
    reset_source_download,
)

log = logging.getLogger("animax.media")


class MediaError(RuntimeError):
    pass


class MediaProcessError(MediaError):
    def __init__(self, program: str, exit_code: int, output: str):
        self.exit_code = exit_code
        self.program = program
        self.summary = error_summary(output)
        self.hint = error_hint(output)
        super().__init__(
            f"{program} exited with code {exit_code}:\n{self.summary}\n{self.hint}"
        )


def normalize_skip_intervals(intervals, duration: float) -> list[tuple[float, float]]:
    clean = []
    for item in intervals or []:
        try:
            start = max(0.0, float(item[0]))
            end = min(duration, float(item[1]))
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
        if start < end and end - start < duration:
            clean.append((start, end))
    clean.sort()
    merged: list[list[float]] = []
    for start, end in clean:
        if merged and start <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def keep_intervals(duration: float, skip_intervals) -> list[tuple[float, float]]:
    cursor = 0.0
    keep = []
    for start, end in normalize_skip_intervals(skip_intervals, duration):
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        keep.append((cursor, duration))
    # Never let malformed or overly broad API timings suppress an entire episode.
    return keep if keep else [(0.0, duration)]


def cut_filter_graph(video_filter: str, segments, has_audio: bool) -> str:
    count = len(segments)
    if count < 1:
        raise ValueError("At least one kept video segment is required.")
    graph = []
    if count == 1:
        graph.append(
            f"[0:v:0]{video_filter},trim=start={segments[0][0]:.3f}:end={segments[0][1]:.3f},setpts=PTS-STARTPTS[v0]"
        )
    else:
        split_labels = "".join(f"[vsrc{i}]" for i in range(count))
        graph.append(f"[0:v:0]{video_filter},split={count}{split_labels}")
        for index, (start, end) in enumerate(segments):
            graph.append(
                f"[vsrc{index}]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]"
            )
    if has_audio:
        if count == 1:
            graph.append(
                f"[0:a:0]atrim=start={segments[0][0]:.3f}:end={segments[0][1]:.3f},asetpts=PTS-STARTPTS[a0]"
            )
        else:
            split_labels = "".join(f"[asrc{i}]" for i in range(count))
            graph.append(f"[0:a:0]asplit={count}{split_labels}")
            for index, (start, end) in enumerate(segments):
                graph.append(
                    f"[asrc{index}]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]"
                )
        inputs = "".join(f"[v{i}][a{i}]" for i in range(count))
        graph.append(f"{inputs}concat=n={count}:v=1:a=1[outv][outa]")
    else:
        inputs = "".join(f"[v{i}]" for i in range(count))
        graph.append(f"{inputs}concat=n={count}:v=1:a=0[outv]")
    return ";".join(graph)


def log_tail(path: Path, start: int = 0, count: int = 16000) -> str:
    with path.open("rb") as file:
        file.seek(max(start, path.stat().st_size - count))
        return file.read().decode("utf-8", "replace")


async def run_process(
    args: list[str],
    cwd: Path,
    *,
    timeout: float = 3600,
    max_bytes: int = 8 * 1024**3,
    disk_root: Path | None = None,
    progress_file: Path | None = None,
    on_progress=None,
) -> str:
    """No shell. Kill/reap subprocesses on timeout, disk exhaustion or cancellation."""
    log = cwd / "process.log"
    start_offset = log.stat().st_size if log.exists() else 0
    started = time.monotonic()
    program = (
        "yt-dlp"
        if len(args) > 2
        and (args[1:3] == ["-m", "yt_dlp"] or Path(args[1]).name == "downloader.py")
        else Path(args[0]).name
    )
    with log.open("ab") as output:
        log.chmod(0o600)
        output.write(
            f"\n=== ANIMAX PROCESS START: {program}; timeout={timeout}s ===\n".encode()
        )
        output.flush()
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=output,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
        )
        waiter = asyncio.create_task(process.wait())
        deadline = asyncio.get_running_loop().time() + timeout
        next_disk_check = 0.0
        try:
            while not waiter.done():
                if asyncio.get_running_loop().time() >= deadline:
                    raise MediaError("Media operation timed out.")
                if time.monotonic() >= next_disk_check:
                    usage = disk_usage(disk_root or cwd)
                    if usage > max_bytes or shutil.disk_usage(cwd).free < 256 * 1024**2:
                        raise MediaError(
                            "Temporary-storage budget exceeded or disk nearly full."
                        )
                    next_disk_check = time.monotonic() + 3
                if progress_file and on_progress and progress_file.exists():
                    try:
                        on_progress(json.loads(progress_file.read_text()))
                    except (ValueError, OSError):
                        pass
                await asyncio.wait({waiter}, timeout=0.5)
            if waiter.result() != 0:
                output.flush()
                program = (
                    "yt-dlp"
                    if len(args) > 2
                    and (
                        args[1:3] == ["-m", "yt_dlp"]
                        or Path(args[1]).name == "downloader.py"
                    )
                    else Path(args[0]).name
                )
                raise MediaProcessError(
                    program, waiter.result(), log_tail(log, start_offset)
                )
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), 5)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        if os.name != "nt":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    await waiter
            if not waiter.done():
                await waiter
            output.write(
                f"\n=== ANIMAX PROCESS END: {program}; exit={process.returncode}; elapsed={time.monotonic() - started:.2f}s ===\n".encode()
            )
            output.flush()
    if progress_file and on_progress and progress_file.exists():
        with contextlib.suppress(ValueError, OSError):
            on_progress(json.loads(progress_file.read_text()))
    return log_tail(log, start_offset)


async def probe(path: Path) -> dict:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        except asyncio.TimeoutError:
            raise MediaError(
                f"FFprobe timed out after 30 seconds while examining {path.name}."
            ) from None
        if stderr:
            with (path.parent / "probe.log").open("ab") as details:
                details.write(b"\n" + path.name.encode() + b":\n" + stderr[-16000:])
            (path.parent / "probe.log").chmod(0o600)
        if process.returncode:
            raise MediaError(
                f"FFprobe could not read {path.name}: {error_summary(stderr.decode('utf-8', 'replace'))}"
            )
        result = json.loads(stdout)
        if not any(s.get("codec_type") == "video" for s in result.get("streams", [])):
            raise MediaError("Download contains no video stream.")
        if float(result["format"].get("duration", 0)) <= 0:
            raise MediaError("Downloaded video has no valid duration.")
        log.info(
            "PROBE_OK file=%s size=%s format=%s duration=%s streams=%s",
            path.name,
            path.stat().st_size,
            result["format"].get("format_name"),
            result["format"].get("duration"),
            [
                (stream.get("codec_type"), stream.get("codec_name"))
                for stream in result.get("streams", [])
            ],
        )
        return result
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.communicate()


async def write_cookie_file(stream: Stream, cookie_file: Path):
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in await stream.cookies():
        lines.append(
            "\t".join(
                [
                    cookie["domain"],
                    str(cookie["domain"].startswith(".")).upper(),
                    cookie["path"],
                    str(cookie["secure"]).upper(),
                    str(max(0, int(cookie.get("expires", 0)))),
                    cookie["name"],
                    cookie["value"],
                ]
            )
        )
    cookie_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cookie_file.chmod(0o600)


async def download(
    stream: Stream,
    work: Path,
    language: str,
    *,
    quality: int = 1080,
    subtitle_mode: str = "burn",
    max_bytes: int = 8 * 1024**3,
    timeout: int = 3600,
    request_interval: float = 0.25,
    on_progress=None,
    encoder_preset: str = "ultrafast",
    encode_threads: int = 2,
    target_mb: int = 450,
    video_crf: int = 19,
    compression_mode: str = "quality",
    compress_all: bool = False,
    lossless: bool = False,
    caption_font: str = "liberation",
    frame_rate: str = "source",
    disk_root: Path | None = None,
    skip_intro_outro: bool = False,
    mal_id: int | None = None,
    episode: int | None = None,
    skip_intervals: list[tuple[float, float]] | None = None,
) -> tuple[Path, str | None]:
    work.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(work).free < 512 * 1024**2:
        raise MediaError("At least 512 MiB of free disk space is required.")
    cookie_file = work / "cookies.txt"
    await write_cookie_file(stream, cookie_file)
    rate_report = work / "rate-limit.json"
    progress_file = work / "progress.json"
    progress_file.unlink(missing_ok=True)
    args = [
        sys.executable,
        str(Path(__file__).with_name("downloader.py")),
        "--mp-request-interval",
        str(request_interval),
        "--mp-rate-limit-report",
        str(rate_report),
        "--mp-progress-file",
        str(progress_file),
        "--ignore-config",
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--socket-timeout",
        "30",
        "--retries",
        "0",
        "--fragment-retries",
        "0",
        "--abort-on-unavailable-fragments",
        "--concurrent-fragments",
        "1",
        "--cookies",
        str(cookie_file),
        "--hls-use-mpegts",
        "--remux-video",
        "mp4",
        "--merge-output-format",
        "mp4",
        "--max-filesize",
        str(max_bytes // 2),
        "-f",
        f"bv*[height<={quality}]+ba/b[height<={quality}]/b",
        "-o",
        str(work / "source.%(ext)s"),
    ]
    for key, value in stream.headers.items():
        args += ["--add-header", f"{key}:{value}"]
    args += ["--", stream.url]
    transfer_started = time.monotonic()
    try:
        removed = discard_fragment_files(work)
        if removed:
            log.warning(
                "DOWNLOAD_RESUME discarded %s uncommitted fragment files; completed-fragment checkpoint retained",
                removed,
            )
        for recovery in range(2):
            rate_report.unlink(missing_ok=True)
            remaining = timeout - (time.monotonic() - transfer_started)
            if remaining <= 0:
                raise MediaError("Media operation timed out.")
            try:
                await run_process(
                    args,
                    work,
                    timeout=remaining,
                    max_bytes=max_bytes,
                    disk_root=disk_root or work,
                    progress_file=progress_file,
                    on_progress=on_progress,
                )
                break
            except MediaProcessError as exc:
                if rate_report.exists():
                    data = json.loads(rate_report.read_text())
                    if data.get("status") == 429:
                        raise RateLimitError(data.get("retry_after")) from exc
                if "429" in exc.summary or "too many requests" in exc.summary.lower():
                    raise RateLimitError() from exc
                if not is_range_error(exc.summary):
                    raise
                removed = reset_source_download(work)
                if recovery:
                    log.warning(
                        "DOWNLOAD_RECOVERY HTTP 416 persists after clean retry; returning to bounded episode retries"
                    )
                    raise
                log.warning(
                    "DOWNLOAD_RECOVERY HTTP 416: removed %s source-state files; retrying once from the beginning",
                    removed,
                )
                with (work / "process.log").open("a") as details:
                    details.write(
                        "\nANIMAX RECOVERY: HTTP 416; restarting source download once without stale local resume state.\n"
                    )
                if on_progress:
                    on_progress({"fraction": 0, "downloaded": 0, "status": "retrying"})
    finally:
        cookie_file.unlink(missing_ok=True)
    source = work / "source.mp4"
    if not source.exists() or not source.stat().st_size:
        raise MediaError(
            "Downloader did not produce source.mp4 (possibly over the disk-size limit)."
        )
    original_info = await probe(source)
    # No intermediate video encode: DAR correction, padding and burn-in share one pass.
    caption_mode = "burn" if language == "dub" else subtitle_mode
    if caption_mode not in {"burn", "mux", "none"}:
        raise ValueError("SUBTITLE_MODE must be burn, mux or none.")
    note = None
    subtitle = None
    if caption_mode != "none":
        track = choose_caption(stream.tracks, language)
        if track is not None:
            subtitle = work / "subtitles.vtt"
            subtitle.write_bytes(await stream.caption_bytes(track))
            fonts = work / "fonts"
            if not fonts.exists():
                assets = Path(__file__).resolve().parent.parent / "assets" / "fonts"
                try:
                    fonts.symlink_to(assets, target_is_directory=True)
                except OSError:
                    shutil.copytree(assets, fonts)
    video = next(
        item for item in original_info["streams"] if item.get("codec_type") == "video"
    )
    filters = video_filter(
        video,
        quality,
        bool(subtitle and caption_mode == "burn"),
        language=language,
        frame_rate=frame_rate,
        caption_font=caption_font,
    )
    log.info(
        "FRAME_RATE source=%s requested=%s", video.get("avg_frame_rate"), frame_rate
    )
    result = work / "episode.mp4"
    safe_limit = SAFE_OUTPUT_MB * 1_000_000
    source_bytes = source.stat().st_size
    if compression_mode not in {"quality", "size"}:
        raise ValueError("Compression mode must be quality or size.")
    if lossless:
        selected_crf, selected_mode = 0, "lossless"
        policy = "lossless override"
    elif compress_all:
        selected_crf, selected_mode = video_crf, compression_mode
        policy = "compress all"
    elif source_bytes > safe_limit:
        selected_crf = video_crf
        selected_mode = "size" if compression_mode == "size" else "safe"
        policy = "source exceeds safe limit"
    else:
        selected_crf, selected_mode = 0, "preserve"
        policy = "preserve below safe limit"
    preserve_pass = selected_mode == "preserve"

    source_duration = float(original_info["format"]["duration"])
    effective_skip_intervals = []
    segments = [(0.0, source_duration)]
    if skip_intro_outro:
        if skip_intervals is None and mal_id and episode:
            skip_intervals = await get_skip_intervals(mal_id, episode, source_duration)
        effective_skip_intervals = normalize_skip_intervals(
            skip_intervals, source_duration
        )
        segments = keep_intervals(source_duration, effective_skip_intervals)
    duration = sum(end - start for start, end in segments)
    if skip_intro_outro and not effective_skip_intervals:
        note = f"No usable AniSkip OP/ED timing was available for episode {episode}; playing the full episode."
    log.info(
        "SKIP_TIMES requested=%s mal_id=%s episode=%s intervals=%s output_duration=%.3f source_duration=%.3f",
        skip_intro_outro,
        mal_id,
        episode,
        effective_skip_intervals,
        duration,
        source_duration,
    )

    has_audio = any(
        item.get("codec_type") == "audio" for item in original_info["streams"]
    )
    filter_graph = (
        cut_filter_graph(filters, segments, has_audio)
        if effective_skip_intervals
        else None
    )

    def encoder_args(crf, mode, destination, file_size_limit=None):
        command = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-threads",
            str(encode_threads),
            "-filter_threads",
            "1",
            "-i",
            str(source),
        ]
        if subtitle and caption_mode == "mux":
            command += ["-i", str(subtitle)]
        if filter_graph:
            command += ["-filter_complex", filter_graph, "-map", "[outv]"]
            if has_audio:
                command += ["-map", "[outa]"]
            else:
                command += ["-an"]
        else:
            command += ["-map", "0:v:0", "-map", "0:a:0?", "-vf", filters]
        command += [
            "-c:v",
            "libx264",
            "-preset",
            encoder_preset,
            "-crf",
            str(crf),
            "-threads",
            str(encode_threads),
            "-pix_fmt",
            "yuv420p",
            "-x264-params",
            "sync-lookahead=0",
            "-vsync",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
        ]
        if crf != 0:
            insertion = command.index("-pix_fmt")
            command[insertion:insertion] = ["-profile:v", "high"]
        if mode in {"size", "safe"}:
            budget = (
                min(target_mb * 1_000_000, safe_limit) if mode == "size" else safe_limit
            )
            ratio = 0.94 if mode == "size" else 0.96
            bitrate = min(8_000_000, int(budget * 8 / duration * ratio - 128000))
            if bitrate < 180000:
                raise MediaLimitError(
                    "The episode is too long to fit the safe output-size limit at usable quality."
                )
            command += ["-maxrate", str(bitrate), "-bufsize", str(bitrate * 2)]
        if subtitle and caption_mode == "mux":
            command += [
                "-map",
                "1:0",
                "-c:s",
                "mov_text",
                "-metadata:s:s:0",
                "language=eng",
                "-disposition:s:0",
                "default",
            ]
        command += ["-movflags", "+faststart"]
        if file_size_limit:
            command += ["-fs", str(file_size_limit)]
        command.append(str(destination))
        return command

    async def encode_pass(crf, mode, file_size_limit=None):
        command = encoder_args(crf, mode, result, file_size_limit)
        log.info(
            "ENCODE_START preset=%s crf=%s threads=%s mode=%s policy=%s source=%sx%s source_fps=%s requested_fps=%s height=%s caption_font=%s",
            encoder_preset,
            crf,
            encode_threads,
            mode,
            policy,
            video["width"],
            video["height"],
            video.get("avg_frame_rate"),
            frame_rate,
            quality,
            caption_font,
        )
        started = time.monotonic()
        await run_process(
            command,
            work,
            timeout=timeout,
            max_bytes=max_bytes,
            disk_root=disk_root or work,
        )
        return time.monotonic() - started

    try:
        encode_seconds = await encode_pass(
            selected_crf, selected_mode, safe_limit if preserve_pass else None
        )
    except MediaProcessError:
        if (
            not preserve_pass
            or not result.exists()
            or result.stat().st_size < safe_limit * 0.90
        ):
            raise
        log.info(
            "PRESERVE_LIMIT_REACHED; retrying near the configured safe output size"
        )
        result.unlink(missing_ok=True)
        preserve_pass = False
        selected_crf = video_crf
        selected_mode = "size" if compression_mode == "size" else "safe"
        policy = "prepared output needs safe-size compression"
        encode_seconds = await encode_pass(selected_crf, selected_mode)

    needs_safe_fit = False
    try:
        result_info = await probe(result)
        output_duration = float(result_info["format"]["duration"])
        needs_safe_fit = abs(output_duration - duration) > 3
    except MediaError:
        if (
            not preserve_pass
            or not result.exists()
            or result.stat().st_size < safe_limit * 0.90
        ):
            raise
        needs_safe_fit = True
        result_info = None

    if preserve_pass and (needs_safe_fit or result.stat().st_size >= safe_limit):
        log.info(
            "PRESERVE_OUTPUT_EXCEEDS_SAFE_LIMIT input_bytes=%s output_bytes=%s safe_bytes=%s; applying size-safe encode",
            source_bytes,
            result.stat().st_size,
            safe_limit,
        )
        result.unlink(missing_ok=True)
        selected_crf = video_crf
        selected_mode = "size" if compression_mode == "size" else "safe"
        policy = "prepared output needs safe-size compression"
        encode_seconds += await encode_pass(selected_crf, selected_mode)
        result_info = await probe(result)
        needs_safe_fit = abs(float(result_info["format"]["duration"]) - duration) > 3

    if needs_safe_fit:
        raise MediaError(
            "MP4 duration changed unexpectedly; refusing a potentially incomplete episode."
        )
    log.info(
        "ENCODE_DONE seconds=%.2f media_seconds=%s bytes=%s effective_crf=%s",
        encode_seconds,
        result_info["format"].get("duration"),
        result.stat().st_size,
        selected_crf,
    )
    if "mp4" not in result_info["format"].get("format_name", "").split(","):
        raise MediaError("Final video is not a real MP4 container.")
    if abs(float(result_info["format"]["duration"]) - duration) > 3:
        raise MediaError(
            "MP4 duration changed unexpectedly; refusing a potentially incomplete episode."
        )
    source.unlink()
    (work / "ready.json").write_text(
        json.dumps(
            {
                "processing_version": PROCESSING_VERSION,
                "file": result.name,
                "language": language,
                "skip_intro_outro": bool(skip_intro_outro),
                "skip_intervals": effective_skip_intervals,
                "source_duration": source_duration,
                "output_duration": duration,
                "quality": quality,
                "subtitle_mode": subtitle_mode,
                "encoder_preset": encoder_preset,
                "fps": frame_rate,
                "source_fps": video.get("avg_frame_rate"),
                "output_fps": next(
                    item
                    for item in result_info["streams"]
                    if item.get("codec_type") == "video"
                ).get("avg_frame_rate"),
                "compression_mode": compression_mode,
                "effective_mode": selected_mode,
                "target_mb": target_mb if compression_mode == "size" else None,
                "video_crf": video_crf,
                "effective_crf": selected_crf,
                "compress_all": bool(compress_all),
                "lossless": bool(lossless),
                "caption_font": caption_font,
                "safe_output_mb": SAFE_OUTPUT_MB,
                "caption_track": track.get("label") if subtitle else None,
                "note": note,
            }
        )
    )
    return result, note


PROCESSING_VERSION = 7


async def cached_video(
    work: Path,
    language: str,
    quality: int,
    subtitle_mode: str,
    encoder_preset="ultrafast",
    target_mb=450,
    video_crf=19,
    compression_mode="quality",
    compress_all=False,
    lossless=False,
    caption_font="liberation",
    frame_rate="source",
    skip_intro_outro=False,
):
    marker = work / "ready.json"
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        if data.get("processing_version") != PROCESSING_VERSION:
            return None
        if data.get("language") != language:
            return None
        if (
            data.get("quality"),
            data.get("subtitle_mode"),
            data.get("encoder_preset"),
        ) != (
            quality,
            subtitle_mode,
            encoder_preset,
        ):
            return None
        if (
            data.get("fps"),
            data.get("target_mb"),
            data.get("video_crf"),
            data.get("compression_mode"),
        ) != (
            frame_rate,
            target_mb if compression_mode == "size" else None,
            video_crf,
            compression_mode,
        ):
            return None
        if (
            data.get("compress_all") != bool(compress_all)
            or data.get("lossless") != bool(lossless)
            or data.get("caption_font") != caption_font
            or data.get("safe_output_mb") != SAFE_OUTPUT_MB
            or data.get("skip_intro_outro", False) != bool(skip_intro_outro)
        ):
            return None
        source = work / data["file"]
        if source.is_symlink() or source.parent.resolve() != work.resolve():
            return None
        await probe(source)
        return source, data.get("note")
    except (ValueError, KeyError, OSError, MediaError):
        return None


class MediaLimitError(MediaError):
    """A whole media file cannot meet the configured output-size safety limit."""
