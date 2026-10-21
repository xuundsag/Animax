"""Safe error summaries and checks using the exact interpreter running the bot."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from importlib import metadata


class SetupError(RuntimeError):
    pass


def redact(text: str) -> str:
    text = re.sub(r"https?://[^\s<>\"\']+", "<redacted URL>", str(text))
    text = re.sub(
        r"(?i)\b(token|signature|sig|key|authorization|cookie)(\s*[=:]\s*)[^\s,;]+",
        r"\1\2<redacted>",
        text,
    )
    return text


def error_summary(text: str, maximum: int = 1000) -> str:
    lines = [line.strip() for line in redact(text).splitlines() if line.strip()]
    important = [
        line
        for line in lines
        if any(
            word in line.lower()
            for word in (
                "error",
                "no module",
                "not found",
                "unsupported",
                "permission denied",
                "not available",
                "failed",
                "invalid",
                "unable",
                "unrecognized",
                "requires python",
            )
        )
    ]
    return (
        "\n".join((important or lines)[-6:])[-maximum:]
        or "No error output was produced."
    )


def error_hint(text: str) -> str:
    lower = text.lower()
    if "no module named" in lower and ("yt_dlp" in lower or "yt-dlp" in lower):
        return "yt-dlp is missing from the bot interpreter. Run pip install -r requirements.txt in the same Python environment."
    if "shared libraries" in lower or "libnspr4" in lower:
        return "Install Chromium OS dependencies: python -m playwright install --with-deps chromium."
    if "range not satisfiable" in lower or "http error 416" in lower:
        return "The CDN rejected a byte-range request. ANIMAX clears stale source resume state for one clean retry; persistent failures need a fresh source resolve or provider recovery."
    if "403" in lower or "forbidden" in lower:
        return "The provider rejected the media request (403). A fresh resolve will be tried; ANIMAX cannot grant access."
    if "429" in lower or "too many requests" in lower:
        return "The media provider is rate-limiting this request. Stop and wait before retrying."
    if "ffmpeg" in lower and ("not found" in lower or "not installed" in lower):
        return "Install FFmpeg and ffprobe on PATH."
    if "requested format" in lower and "not available" in lower:
        return "The provider did not advertise the requested format. Check the episode, language and provider availability."
    if "certificate" in lower or "ssl" in lower:
        return "Check system time, trusted CA certificates and HTTPS connectivity. Certificate checks have not been disabled."
    if "unrecognized" in lower or "no such option" in lower:
        return "The downloader CLI is outdated or incompatible. Reinstall requirements in the bot virtual environment."
    if "timed out" in lower or "timeout" in lower:
        return "Check media-CDN connectivity from this server; the browser page working does not prove its CDN is reachable."
    return (
        "Read logs/last-error-MAL-LANGUAGE.log for the complete local process output."
    )


def dependency_report() -> dict:
    """Check local CPU-only runtimes without logging in or making network requests."""
    if sys.version_info < (3, 10):
        raise SetupError("Python 3.10+ is required; 3.11+ is recommended.")
    report = {
        "python": sys.version.split()[0],
        "interpreter": sys.executable,
        "packages": {},
    }
    for name in (
        "playwright",
        "yt-dlp",
        "aiohttp",
        "python-dotenv",
        "rich",
        "rapidfuzz",
    ):
        try:
            report["packages"][name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            raise SetupError(
                f"{name} is missing from {sys.executable}. Run pip install -r requirements.txt in the same environment."
            ) from None
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            raise SetupError(
                f"{executable} is missing from PATH. Install a current FFmpeg build."
            )
        result = subprocess.run(
            [executable, "-version"], capture_output=True, text=True, timeout=20
        )
        if result.returncode:
            raise SetupError(f"{executable} failed: {error_summary(result.stderr)}")
        report[executable] = result.stdout.splitlines()[0]
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    filters = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if (
        encoders.returncode
        or "libx264" not in encoders.stdout
        or "aac" not in encoders.stdout
    ):
        raise SetupError("FFmpeg must include the CPU libx264 and AAC encoders.")
    if filters.returncode or "subtitles" not in filters.stdout:
        raise SetupError(
            "FFmpeg must include the subtitles/libass filter to burn captions."
        )
    if "azmq" not in filters.stdout:
        raise SetupError(
            "FFmpeg must include the azmq/libzmq filter used by the Go Live audio pipeline."
        )
    report["ffmpeg_codecs"] = (
        "libx264 + AAC + subtitles/libass + azmq; hardware acceleration is not required"
    )
    node = shutil.which("node")
    if not node:
        raise SetupError(
            "Node.js 22.4+ is required for the DAVE-capable Go Live sidecar."
        )
    node_version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, timeout=10
    )
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", node_version.stdout)
    version = tuple(map(int, match.groups())) if match else (0, 0, 0)
    if node_version.returncode or version < (22, 4, 0):
        raise SetupError(
            f"Node.js 22.4+ is required; found {node_version.stdout.strip() or 'unknown'}."
        )
    report["node"] = node_version.stdout.strip()
    if not shutil.which("npm"):
        raise SetupError("npm is required to install the Go Live sidecar packages.")
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise SetupError(
            "yt-dlp cannot start in the bot interpreter: "
            + error_summary(result.stdout + result.stderr)
        )
    report["downloader_cli"] = result.stdout.strip()
    return report
