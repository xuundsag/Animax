"""Human-facing terminal output; detailed traces stay in rotating private logs."""

from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, TextColumn, TimeElapsedColumn
from rich.text import Text


def nice_error(exc):
    text = str(exc).lower()
    status = getattr(exc, "status", None)
    if type(exc).__name__ in {"UploadFailure", "JournalCommitFailure"}:
        return (
            "ANX-UPLOAD-REVIEW"
            if getattr(exc, "outcome_unknown", False)
            else "ANX-UPLOAD"
        ), str(exc)
    if "another animax instance" in text:
        return (
            "ANX-RUNNING",
            "Another ANIMAX instance holds the lock. Stop the existing bot process first.",
        )
    if "missing from" in text or "ffmpeg" in text and "missing" in text:
        return (
            "ANX-SETUP",
            "A runtime dependency is missing. Install FFmpeg and run pip install -r requirements.txt in the same Python environment.",
        )
    if "permission denied" in text:
        return (
            "ANX-FILES",
            "The process cannot access a required file. Check project ownership and directory permissions.",
        )
    if "must be" in text or "invalid anime.json" in text:
        return (
            "ANX-CONFIG",
            "A configuration or history file is invalid. The precise field is in logs/bot.log; history was not erased.",
        )
    if status == 429 and type(exc).__module__.startswith("discord"):
        return (
            "ANX-DISCORD429",
            "Discord rate-limited the upload. The job stopped safely; wait before resuming.",
        )
    if "429" in text or status == 429:
        return (
            "ANX-CDN429",
            "The video server asked us to slow down. Cooldown saved; no episode hopping.",
        )
    if "403" in text or status == 403:
        return (
            "ANX-ACCESS",
            "Access was refused. Check source availability or channel permissions; repeated retries will not grant access.",
        )
    if status == 413 or "whole episode is" in text:
        return (
            "ANX-SIZE",
            "The whole video exceeds the allowed upload size. Compression may help; splitting is disabled.",
        )
    if "disk" in text or "storage" in text or "space" in text:
        return (
            "ANX-DISK",
            "Not enough temporary disk space. Free disk space or increase the configured storage budget.",
        )
    if "timed out" in text or "timeout" in text or "connection" in text:
        return (
            "ANX-NET",
            "The connection stalled. A bounded retry may recover it; check your server network if it persists.",
        )
    if "libnspr" in text or "shared libraries" in text or "browsertype.launch" in text:
        return (
            "ANX-BROWSER",
            "Chromium cannot start. Install Chromium system dependencies with python -m playwright install --with-deps chromium.",
        )
    if "subtitle" in text:
        return (
            "ANX-SUBTITLE",
            "Subtitle processing failed. See the episode log. Burn-in is never silently replaced with soft captions.",
        )
    if "no playable" in text or "unmapped" in text or "404" in text:
        return (
            "ANX-SOURCE",
            "This episode/language could not be resolved. It may be missing, unmapped or unavailable.",
        )
    if "token" in text or "login" in text:
        return (
            "ANX-LOGIN",
            "Login failed. Check the token locally in .env; never post it in chat.",
        )
    return (
        "ANX-MEDIA",
        "This operation failed. The detailed reason is saved in the local logs; the job will not silently mark it complete.",
    )


class Terminal:
    def __init__(self, quiet=False):
        self.console = Console(markup=False, quiet=quiet)
        self.tty = self.console.is_terminal and not quiet
        self.progress = Progress(
            TextColumn("{task.description}", markup=False, style="cyan"),
            TextColumn("{task.fields[label]}", markup=False),
            TimeElapsedColumn(),
            console=self.console,
            transient=False,
            disable=not self.tty,
        )
        self.tasks = {}
        self.last = {}

    def banner(self):
        self.console.print(
            Text(
                "ANIMAX 2.4.0  |  .help for commands  |  Ctrl+C saves progress",
                style="bold cyan",
            )
        )

    def event(self, message, style="cyan"):
        self.console.print(
            Text(f"[{time.strftime('%H:%M:%S')}] {message}", style=style)
        )

    def start(self, title, key="default", worker_id=0):
        self.finish(key)
        self.progress.start()
        self.tasks[key] = self.progress.add_task(
            f"W{worker_id} : {title}" if worker_id else title,
            total=100,
            label="Resolving",
        )

    def update(self, job):
        fraction = job.progress.get("fraction")
        percent = (
            min(100, max(0, fraction * 100))
            if isinstance(fraction, (int, float))
            else None
        )
        if job.phase == "Enhancing":
            elapsed = max(0, int(time.monotonic() - job.stage_started))
            label = f"Enhancing | {elapsed // 60}:{elapsed % 60}"
        elif job.phase == "Done":
            size = (
                f"{job.output_size_bytes / 1_000_000:.1f} MB"
                if job.output_size_bytes is not None
                else "size unavailable"
            )
            label = f"Done | {size}"
        else:
            label = job.phase
            if percent is not None and job.phase == "Downloading":
                label += f" | {percent:.1f}%"
                speed = job.progress.get("speed") or 0
                if speed:
                    label += f" | {speed / 1024**2:.2f} MiB/s"
        task = self.tasks.get(job.id, self.tasks.get("default"))
        prefix = f"W{job.worker_id} : " if job.worker_id else ""
        if task is not None:
            self.progress.update(
                task,
                description=f"{prefix}E{job.current} | {job.title[:32]}",
                completed=percent or 0,
                label=label,
            )
        if not self.tty:
            bucket = int((percent or 0) // 10)
            last_stage, last_bucket = self.last.get(job.id, (None, -1))
            if job.phase != last_stage or bucket > last_bucket:
                self.event(f"{prefix}{job.title} | E{job.current} | {label}")
                self.last[job.id] = job.phase, bucket

    def error(self, exc, log_name="logs/bot.log"):
        code, message = nice_error(exc)
        self.event(f"{code} | {message} | {log_name}", "red")

    def finish(self, key=None):
        for entry in list(self.tasks) if key is None else [key]:
            task = self.tasks.pop(entry, None)
            if task is not None:
                self.progress.remove_task(task)
            self.last.pop(entry, None)
        if not self.tasks:
            self.progress.stop()


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
    (logs / "bot.log").chmod(0o600)
