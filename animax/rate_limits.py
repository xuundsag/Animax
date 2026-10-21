"""Rate-limit helpers; ANIMAX never attempts to bypass provider cooldowns."""

from __future__ import annotations

import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime


class RateLimitError(RuntimeError):
    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__(
            "Media provider returned HTTP 429 (Too Many Requests). Stop and wait before retrying."
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
