"""Small terminal/logging surface for the single continuous Go Live session."""

from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.text import Text


def nice_error(exc):
    text = str(exc).casefold()
    if "another animax" in text:
        return "ANX-RUNNING", "Another ANIMAX instance is active; stop it first."
    if "node.js" in text or "sidecar" in text or "dave" in text:
        return (
            "ANX-NODE",
            "Check Node.js 22.4+, the pinned Go Live packages and logs/stream-bridge.log.",
        )
    if "ffmpeg" in text or "ffprobe" in text:
        return (
            "ANX-FFMPEG",
            "Install FFmpeg/FFprobe with libx264, AAC, subtitles/libass and azmq/libzmq.",
        )
    if "subtitle" in text or "caption" in text:
        return (
            "ANX-CAPTION",
            "Caption retrieval or burn-in failed; inspect logs/bot.log and the episode process log.",
        )
    if "429" in text or "too many requests" in text:
        return (
            "ANX-RATE",
            "The media provider rate-limited the request; wait before retrying.",
        )
    if "disk" in text or "storage" in text or "space" in text:
        return "ANX-DISK", "Free temporary disk space or increase MAX_TEMP_GIB."
    if "timed out" in text or "timeout" in text or "connection" in text:
        return (
            "ANX-NET",
            "Check provider/API connectivity and retry when the service is available.",
        )
    if "voice" in text or "channel" in text:
        return (
            "ANX-VOICE",
            "Join the intended server voice channel before running .stream.",
        )
    if "token" in text or "login" in text:
        return (
            "ANX-LOGIN",
            "Check DISCORD_TOKEN privately in .env; never post it in chat.",
        )
    return (
        "ANX-STREAM",
        "The stream stopped safely. See logs/bot.log and logs/stream-bridge.log.",
    )


class Terminal:
    def __init__(self, quiet=False):
        self.console = Console(markup=False, quiet=quiet)

    def banner(self):
        self.console.print(
            Text("ANIMAX 2.4.3 | DAVE-capable Go Live | CPU mode", style="bold cyan")
        )

    def event(self, message, style="cyan"):
        self.console.print(
            Text(f"[{time.strftime('%H:%M:%S')}] {message}", style=style)
        )

    def error(self, exc, log_name="logs/bot.log"):
        code, message = nice_error(exc)
        self.event(f"{code} | {message} | {log_name}", "red")

    def finish(self, *_args, **_kwargs):
        return None


def setup_logging(root: Path):
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    handler = RotatingFileHandler(logs / "bot.log", maxBytes=5 * 1024**2, backupCount=4)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)
    for name in ("logs/bot.log", "logs/stream-bridge.log"):
        path = root / name
        if path.exists():
            path.chmod(0o600)
