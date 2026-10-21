"""Discord-attachment download and FFmpeg validation for the Go Live stream."""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .diagnostics import error_summary
from .disk import disk_usage

log = logging.getLogger("animax.stream_media")

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".m4v",
    ".wmv",
    ".flv",
    ".mpeg",
    ".mpg",
    ".ts",
    ".3gp",
}
VIDEO_CONTENT_TYPES = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "video/avi": ".avi",
    "video/mpeg": ".mpeg",
    "video/x-m4v": ".m4v",
    "video/mp2t": ".ts",
    "video/x-flv": ".flv",
    "video/x-ms-wmv": ".wmv",
    "video/3gpp": ".3gp",
}
_ALLOWED_DISCORD_CDN_HOSTS = {
    "cdn.discordapp.com",
    "media.discordapp.net",
    "cdn.discordapp.net",
}


class MediaError(RuntimeError):
    pass


def is_video_attachment(filename: str, content_type: str | None = None) -> bool:
    """Recognize Discord video attachments by MIME type or their filename suffix."""
    suffix = Path(str(filename or "")).suffix.casefold()
    mime = str(content_type or "").split(";", 1)[0].strip().casefold()
    return mime.startswith("video/") or suffix in VIDEO_EXTENSIONS


def attachment_extension(filename: str, content_type: str | None = None) -> str:
    suffix = Path(str(filename or "")).suffix.casefold()
    if suffix in VIDEO_EXTENSIONS:
        return suffix
    mime = str(content_type or "").split(";", 1)[0].strip().casefold()
    return VIDEO_CONTENT_TYPES.get(mime, ".video")


def _trusted_discord_cdn(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in _ALLOWED_DISCORD_CDN_HOSTS
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


async def download_discord_attachment(
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    timeout: int | float,
    expected_size: int | None = None,
    disk_root: Path | None = None,
    on_progress=None,
) -> int:
    """Stream one fresh Discord CDN URL to disk without buffering the video in RAM."""
    if not _trusted_discord_cdn(url):
        raise MediaError("The saved upload did not provide a trusted Discord CDN URL.")
    if expected_size is not None:
        try:
            expected_size = int(expected_size)
        except (TypeError, ValueError):
            expected_size = None
        if expected_size is not None and expected_size < 0:
            expected_size = None
    if expected_size and expected_size > max_bytes:
        raise MediaError(
            f"Discord attachment is {expected_size / 1_000_000:.1f} MB, above the configured stream-cache limit."
        )

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink():
        raise MediaError("Refusing to overwrite a symbolic-link stream-cache file.")
    partial = destination.with_name(destination.name + ".part")
    if partial.is_symlink():
        raise MediaError("Refusing to write through a symbolic-link partial file.")
    partial.unlink(missing_ok=True)

    root = Path(disk_root or destination.parent)
    used_before = disk_usage(root)
    free = shutil.disk_usage(destination.parent).free
    if free < 256 * 1024**2:
        raise MediaError(
            "At least 256 MiB of free disk space is required for streaming."
        )
    if expected_size and used_before + expected_size > max_bytes:
        raise MediaError("The configured stream-cache disk budget would be exceeded.")

    started = time.monotonic()
    received = 0
    total = expected_size or 0
    client_timeout = aiohttp.ClientTimeout(
        total=max(1, float(timeout)),
        connect=min(30, max(1, float(timeout))),
        sock_read=90,
    )
    try:
        async with aiohttp.ClientSession(
            timeout=client_timeout, trust_env=True
        ) as client:
            async with client.get(url) as response:
                if response.status != 200:
                    raise MediaError(
                        f"Discord CDN returned HTTP {response.status} while downloading the uploaded episode."
                    )
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        header_size = int(content_length)
                        if header_size >= 0:
                            total = header_size
                    except ValueError:
                        pass
                if total > max_bytes:
                    raise MediaError(
                        f"Discord attachment is {total / 1_000_000:.1f} MB, above the configured stream-cache limit."
                    )
                if total and used_before + total > max_bytes:
                    raise MediaError(
                        "The configured stream-cache disk budget would be exceeded."
                    )

                with partial.open("wb") as output:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > max_bytes or used_before + received > max_bytes:
                            raise MediaError(
                                "The configured stream-cache disk budget was exceeded while downloading."
                            )
                        output.write(chunk)
                        if on_progress:
                            result = on_progress(received, total)
                            if asyncio.iscoroutine(result):
                                await result
                    output.flush()

                if expected_size and received != expected_size:
                    raise MediaError(
                        f"Discord attachment download was incomplete ({received} of {expected_size} bytes)."
                    )
                if total and received != total:
                    raise MediaError(
                        f"Discord CDN download was incomplete ({received} of {total} bytes)."
                    )
                if received <= 0:
                    raise MediaError("Discord returned an empty video attachment.")
                if destination.exists():
                    destination.unlink()
                partial.replace(destination)
                destination.chmod(0o600)
                log.info(
                    "DISCORD_ATTACHMENT_DOWNLOADED file=%s bytes=%s seconds=%.2f",
                    destination.name,
                    received,
                    time.monotonic() - started,
                )
                return received
    except asyncio.CancelledError:
        raise
    except MediaError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError) as exc:
        raise MediaError(
            f"Could not download the Discord attachment ({type(exc).__name__}: {exc})."
        ) from exc
    finally:
        partial.unlink(missing_ok=True)


async def _communicate_process(
    args: list[str], *, timeout: float
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise MediaError(
                f"FFmpeg operation timed out after {timeout:g} seconds."
            ) from None
        return process.returncode or 0, stdout, stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


async def probe(path: Path) -> dict:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise MediaError("Downloaded Discord video is missing or unsafe.")
    args = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    try:
        code, stdout, stderr = await _communicate_process(args, timeout=45)
    except FileNotFoundError as exc:
        raise MediaError("FFprobe is not installed or is missing from PATH.") from exc
    if stderr:
        with (path.parent / "probe.log").open("ab") as details:
            details.write(
                b"\n" + path.name.encode("utf-8", "replace") + b":\n" + stderr[-16000:]
            )
        (path.parent / "probe.log").chmod(0o600)
    if code:
        raise MediaError(
            f"FFprobe could not read {path.name}: {error_summary(stderr.decode('utf-8', 'replace'))}"
        )
    try:
        result = __import__("json").loads(stdout)
    except (ValueError, TypeError) as exc:
        raise MediaError(f"FFprobe returned invalid metadata for {path.name}.") from exc
    streams = result.get("streams", [])
    if not any(stream.get("codec_type") == "video" for stream in streams):
        raise MediaError("The Discord attachment contains no video stream.")
    durations = [result.get("format", {}).get("duration")]
    durations.extend(stream.get("duration") for stream in streams)
    valid_duration = 0.0
    for value in durations:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number):
            valid_duration = max(valid_duration, number)
    if valid_duration <= 0:
        raise MediaError("The Discord video has no valid duration.")
    result.setdefault("format", {})["duration"] = str(valid_duration)
    log.info(
        "PROBE_OK file=%s size=%s format=%s duration=%s streams=%s",
        path.name,
        path.stat().st_size,
        result["format"].get("format_name"),
        result["format"].get("duration"),
        [(stream.get("codec_type"), stream.get("codec_name")) for stream in streams],
    )
    return result


async def _can_decode_frame(path: Path, timeout: float = 90) -> bool:
    common = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
    ]
    attempts = [
        ["-ignore_editlist", "1", "-probesize", "100M", "-analyzeduration", "100M"],
        ["-probesize", "100M", "-analyzeduration", "100M"],
    ]
    for input_options in attempts:
        try:
            code, _stdout, _stderr = await _communicate_process(
                [
                    *common,
                    *input_options,
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-an",
                    "-f",
                    "null",
                    "-",
                ],
                timeout=timeout,
            )
            if code == 0:
                return True
        except (FileNotFoundError, MediaError):
            continue
    return False


async def ensure_playable(
    path: Path,
    *,
    max_bytes: int,
    timeout: int | float,
    disk_root: Path | None = None,
) -> tuple[Path, dict]:
    """Check a decoded video frame; remux a broken container once before streaming."""
    path = Path(path)
    info = await probe(path)
    if await _can_decode_frame(path):
        return path, info

    repair = path.with_name(f"{path.stem}.repaired.mkv")
    if repair.exists() and not repair.is_symlink():
        try:
            repaired_info = await probe(repair)
            if await _can_decode_frame(repair):
                log.info("Using cached repaired stream file %s", repair.name)
                return repair, repaired_info
        except MediaError:
            repair.unlink(missing_ok=True)

    root = Path(disk_root or path.parent)
    if disk_usage(root) + path.stat().st_size > max_bytes:
        raise MediaError(
            "Not enough configured disk budget to repair the uploaded video."
        )
    if (
        shutil.disk_usage(path.parent).free
        < min(path.stat().st_size, 512 * 1024**2) + 128 * 1024**2
    ):
        raise MediaError("Not enough free disk space to repair the uploaded video.")

    temporary = repair.with_name(repair.name + ".part")
    temporary.unlink(missing_ok=True)
    log.warning(
        "SOURCE_FRAME_DECODE_FAILED file=%s; attempting a bounded FFmpeg remux repair",
        path.name,
    )
    args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostdin",
        "-v",
        "warning",
        "-ignore_editlist",
        "1",
        "-probesize",
        "100M",
        "-analyzeduration",
        "100M",
        "-fflags",
        "+genpts+igndts+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-f",
        "matroska",
        str(temporary),
    ]
    try:
        code, _stdout, stderr = await _communicate_process(
            args, timeout=max(90, min(float(timeout), 600))
        )
        if code or not temporary.exists() or temporary.stat().st_size < 1024:
            raise MediaError(
                "FFmpeg could not repair the uploaded video container: "
                + error_summary(stderr.decode("utf-8", "replace"))
            )
        if disk_usage(root) + temporary.stat().st_size > max_bytes:
            raise MediaError(
                "The repaired upload would exceed the configured disk budget."
            )
        temporary.replace(repair)
        repaired_info = await probe(repair)
        if not await _can_decode_frame(repair):
            raise MediaError(
                "The Discord attachment still cannot decode after one FFmpeg repair."
            )
        log.info(
            "REPAIR_OK source=%s output=%s bytes=%s",
            path.name,
            repair.name,
            repair.stat().st_size,
        )
        return repair, repaired_info
    except FileNotFoundError as exc:
        raise MediaError("FFmpeg is not installed or is missing from PATH.") from exc
    finally:
        temporary.unlink(missing_ok=True)
