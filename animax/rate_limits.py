"""Respect provider rate limits; never rotate IPs or bypass a cooldown."""

from __future__ import annotations

import json
import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


class RateLimitError(RuntimeError):
    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__(
            "Media provider returned HTTP 429 (Too Many Requests). Pause this entire job before retrying."
        )


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    if not value:
        return None
    now = time.time() if now is None else now
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - now
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, math.ceil(seconds)) if math.isfinite(seconds) else None


def cooldown_seconds(number: int, base: int, retry_after: float | None) -> float:
    # Never shorten the provider's Retry-After, even when it exceeds our normal cap.
    return max(min(base * 2 ** min(max(number - 1, 0), 20), 3600), retry_after or 0)


class Cooldown:
    def __init__(self, path: Path):
        self.path = path
        self.until = 0.0
        self.interval = 0.0
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.until = float(data.get("until", 0))
            self.interval = float(data.get("interval", 0))
            if not math.isfinite(self.until):
                raise ValueError(
                    "Invalid CDN cooldown timestamp; inspect cdn_cooldown.json."
                )

    def remaining(self) -> float:
        return max(0, self.until - time.time())

    def defer(self, seconds: float):
        self.until = max(self.until, time.time() + seconds)
        self.save()

    def slow_down(self, base=0.25):
        self.interval = min(8, max(2, (self.interval or base) * 2))
        self.save()

    def relax(self, base=0.25):
        if self.remaining() > 0:
            return
        self.interval = max(base, (self.interval or base) * 0.8)
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "until": self.until,
                    "interval": self.interval,
                    "reason": "Adaptive CDN pacing",
                }
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)
