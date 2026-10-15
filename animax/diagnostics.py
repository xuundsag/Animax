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
        return "The CDN is rate-limiting this IP. Respect the saved cooldown; no episode hopping or immediate fragment retry."
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
    """No Discord login, no network requests, no credentials in output."""
    if sys.version_info < (3, 10):
        raise SetupError(
            "Python 3.10+ is required; 3.11+ is recommended. Use the same interpreter to install dependencies and run the bot."
        )
    report = {
        "python": sys.version.split()[0],
        "interpreter": sys.executable,
        "packages": {},
    }
    for name in ("discord.py-self", "playwright", "yt-dlp", "aiohttp", "python-dotenv"):
        try:
            report["packages"][name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            raise SetupError(
                f"{name} is missing from {sys.executable}. Run pip install -r requirements.txt in the same Python environment."
            ) from None
    for conflicting in ("discord.py", "py-cord"):
        try:
            metadata.version(conflicting)
        except metadata.PackageNotFoundError:
            pass
        else:
            raise SetupError(
                f"{conflicting} conflicts with discord.py-self. Remove the conflicting package from this Python environment."
            )
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            raise SetupError(f"{executable} is missing from PATH. Install FFmpeg.")
        result = subprocess.run(
            [executable, "-version"], capture_output=True, text=True, timeout=20
        )
        if result.returncode:
            raise SetupError(f"{executable} failed: {error_summary(result.stderr)}")
        report[executable] = result.stdout.splitlines()[0]
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
