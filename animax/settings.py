"""Small, worker-free configuration surface for CPU-only Go Live streaming."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

SAFE_OUTPUT_MB = 950


@dataclass
class SettingField:
    attribute: str
    environment: str
    kind: type = str
    choices: tuple = ()
    minimum: float | None = None
    maximum: float | None = None
    unit: int = 1

    def parse(self, value: str):
        text = str(value).strip().lower()
        try:
            if self.kind is bool:
                if text not in {"on", "off", "true", "false", "yes", "no", "1", "0"}:
                    raise ValueError
                parsed = text in {"on", "true", "yes", "1"}
            else:
                parsed = self.kind(text)
                if self.kind in {int, float}:
                    if not math.isfinite(parsed):
                        raise ValueError
                    if self.minimum is not None and parsed < self.minimum:
                        raise ValueError
                    if self.maximum is not None and parsed > self.maximum:
                        raise ValueError
                if self.choices and parsed not in self.choices:
                    raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f"Valid options: {self.options}.") from None
        return parsed * self.unit if self.unit != 1 else parsed

    @property
    def options(self):
        if self.kind is bool:
            return "true, false, on, off"
        if self.choices:
            return ", ".join(map(str, self.choices))
        if self.kind is int:
            return f"{int(self.minimum)} to {int(self.maximum)}"
        return f"{self.minimum:g} to {self.maximum:g}"

    def display(self, settings: "Settings"):
        value = getattr(settings, self.attribute)
        if self.kind is bool:
            return "on" if value else "off"
        if self.unit != 1:
            value //= self.unit
        return str(value)


@dataclass
class Settings:
    quality: int = 1080
    caption_font: str = "liberation"
    dub_fallback: bool = True
    keep_stream_cache: bool = False
    max_temp_bytes: int = 8 * 1024**3
    operation_timeout: int = 7200
    resolve_timeout: int = 60
    request_interval: float = 0.25
    headless: bool = True
    encoder_preset: str = "ultrafast"
    encode_threads: int = 2
    target_mb: int = 450
    video_crf: int = 19
    compression_mode: str = "quality"
    compress_all: bool = False
    lossless: bool = False
    frame_rate: str = "source"
    stream_bitrate_kbps: int = 5000
    prefix: str = "."

    @classmethod
    def from_env(cls, path: Path | None = None):
        settings = cls()
        for name, spec in FIELDS.items():
            if spec.environment in os.environ:
                setattr(
                    settings, spec.attribute, spec.parse(os.environ[spec.environment])
                )
        if path is not None and path.exists():
            try:
                values = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(
                    "settings.json must be a readable JSON object."
                ) from exc
            if not isinstance(values, dict):
                raise ValueError("settings.json must contain a JSON object.")
            removed_legacy = {
                "workers",
                "max_workers",
                "max_active_workers",
                "upload_mb",
                "upload_limit_mb",
                "upload_mode",
                "upload_timeout",
                "upload_attempts",
                "upload_fallback",
                "upload_check_timeout",
                "status_interval",
                "auto_resume",
                "delete_trigger",
                "delete_command_triggers",
                "log_channel",
                "channel_id",
                "max_episodes",
                "max_consecutive_failures",
                "rate_limit_retries",
                "rate_limit_cooldown",
                "attempts",
                "episode_attempts",
                "subtitle_mode",
                "subtitles",
                "subtitle_fallback",
                "auto_compress",
                "max_size",
                "output_max_mb",
                "quality",
                "target_mb",
                "safe_output_mb",
                "prepare_timeout",
            }
            for name, value in values.items():
                key = ALIASES.get(name, name)
                if key not in FIELDS:
                    if name.casefold() in removed_legacy:
                        continue
                    raise ValueError(f"Unknown setting '{name}'.")
                setattr(settings, FIELDS[key].attribute, FIELDS[key].parse(str(value)))
        # The Go Live requirement is intentionally fixed; exposing other output dimensions
        # here would make the command promise different from the actual stream profile.
        settings.quality = 1080
        return settings

    def set(self, name: str, value: str):
        key = ALIASES.get(name.casefold(), name.casefold())
        if key not in FIELDS:
            raise ValueError("Unknown setting. Use .settings to list valid settings.")
        setattr(self, FIELDS[key].attribute, FIELDS[key].parse(value))
        return key


FIELDS = {
    "caption_font": SettingField(
        "caption_font",
        "CAPTION_FONT",
        choices=("liberation", "dejavu", "dejavu-serif", "dejavu-mono"),
    ),
    "dub_fallback": SettingField("dub_fallback", "DUB_FALLBACK", bool),
    "keep_stream_cache": SettingField("keep_stream_cache", "KEEP_STREAM_CACHE", bool),
    "disk_gib": SettingField(
        "max_temp_bytes", "MAX_TEMP_GIB", int, minimum=1, maximum=1000, unit=1024**3
    ),
    "request_interval": SettingField(
        "request_interval", "REQUEST_INTERVAL", float, minimum=0.1, maximum=10
    ),
    "resolve_timeout": SettingField(
        "resolve_timeout", "RESOLVE_TIMEOUT", int, minimum=15, maximum=300
    ),
    "operation_timeout": SettingField(
        "operation_timeout", "OPERATION_TIMEOUT", int, minimum=60, maximum=86400
    ),
    "headless": SettingField("headless", "HEADLESS", bool),
    "preset": SettingField(
        "encoder_preset",
        "VIDEO_PRESET",
        choices=("ultrafast", "superfast", "veryfast", "faster", "fast", "medium"),
    ),
    "threads": SettingField(
        "encode_threads", "ENCODE_THREADS", int, minimum=1, maximum=16
    ),
    "compressall": SettingField("compress_all", "COMPRESS_ALL", bool),
    "lossless": SettingField("lossless", "LOSSLESS", bool),
    "crf": SettingField("video_crf", "VIDEO_CRF", int, minimum=16, maximum=28),
    "compression": SettingField(
        "compression_mode", "COMPRESSION_MODE", choices=("quality", "size")
    ),
    "fps": SettingField(
        "frame_rate",
        "FRAME_RATE",
        choices=("source", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60"),
    ),
    "stream_bitrate": SettingField(
        "stream_bitrate_kbps", "STREAM_BITRATE_KBPS", int, minimum=2500, maximum=8000
    ),
}
ALIASES = {
    "font": "caption_font",
    "encoder_preset": "preset",
    "video_preset": "preset",
    "encode_threads": "threads",
    "max_temp_gib": "disk_gib",
    "keep_cache": "keep_stream_cache",
    "cache": "keep_stream_cache",
    "video_crf": "crf",
    "compress_all": "compressall",
    "compression_mode": "compression",
    "frame_rate": "fps",
    "stream_bitrate_kbps": "stream_bitrate",
    "bitrate": "stream_bitrate",
}


def settings_text(settings: Settings) -> str:
    lines = ["ANIMAX stream settings (output is fixed at 1080p, 16:9):"]
    for name, spec in FIELDS.items():
        lines.append(f"{name}: {spec.display(settings)}")
    lines.extend(
        [
            "skip-times: chosen per .stream command (Yes|No); unavailable timings mean no skip",
            "voice: command author’s current voice channel",
            "workers: removed (one active stream; one episode prefetch slot)",
        ]
    )
    return "\n".join(lines)
