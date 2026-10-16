from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field

import discord


@dataclass
class Job:
    mal: int
    language: str
    start: int
    end: int
    channel_id: int
    title: str = ""
    worker_id: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    current: int = 0
    phase: str = "Queued"
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    output_size_bytes: int | None = None
    cooldown_until: float = 0
    progress: dict = field(default_factory=dict)
    error: str = ""
    notice: str = ""
    actual_language: str = ""
    fallback_reason: str = ""
    fallback_notice_sent: bool = False
    stage_started: float = field(default_factory=time.monotonic)

    def text(self):
        return self.episode_text()

    def episode_text(self):
        title = discord.utils.escape_markdown(self.title or f"MAL {self.mal}")
        phase = self.phase
        for prefix, label in (
            ("Resolving", "Resolving"),
            ("Using verified", "Preparing upload"),
            ("Fitting", "Compressing"),
            ("CDN cooldown", "CDN cooldown"),
        ):
            if phase.startswith(prefix):
                phase = label
                break

        progress = "..."
        fraction = self.progress.get("fraction")
        if phase == "Enhancing":
            seconds = max(0, int(time.monotonic() - self.stage_started))
            progress = f"`{seconds // 60}:{seconds % 60}`"
        elif phase in {"Done", "Uploaded"}:
            phase = "Done"
            progress = (
                f"`{self.output_size_bytes / 1_000_000:.1f} MB`"
                if self.output_size_bytes is not None
                else "`size unavailable`"
            )
        elif phase in {"Queued", "Preparing", "Resolving"}:
            progress = "0%"
        elif phase == "Downloading" and isinstance(fraction, (int, float)):
            if math.isfinite(fraction):
                progress = f"{min(100, max(0, int(fraction * 100)))}%"

        parts = [f"**{title}**", f"`E{self.current or self.start}`", phase, progress]
        if self.cooldown_until > time.time():
            parts.append(f"{math.ceil(self.cooldown_until - time.time())}s")
        if self.error:
            parts.append(" ".join(self.error.split())[:260])
        prefix = f"W{self.worker_id} : " if self.worker_id else ""
        return prefix + " | ".join(parts)
