from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass
class Settings:
    channel_id: int = 0
    upload_mb: int = 1000
    quality: int = 1080
    subtitle_mode: str = "burn"
    max_episodes: int = 1000
    max_temp_bytes: int = 8 * 1024**3
    operation_timeout: int = 3600
    resolve_timeout: int = 60
    delay: int = 2
    attempts: int = 3
    max_consecutive_failures: int = 3
    headless: bool = True
    request_interval: float = 0.25
    rate_limit_cooldown: int = 300
    rate_limit_retries: int = 2
    status_interval: int = 8
    auto_resume: bool = True
    auto_compress: bool = False
    subtitle_fallback: bool = False
    keep_downloads: bool = False
    upload_timeout: int = 1800
    upload_mode: str = "auto"
    upload_attempts: int = 2
    upload_fallback: bool = True
    upload_check_timeout: int = 60
    prepare_timeout: int = 300
    delete_trigger: bool = True
    encoder_preset: str = "veryfast"
    encode_threads: int = 4
    target_mb: int = 450
    output_max_mb: int = 1000
    video_crf: int = 19
    dub_fallback: bool = True
    compression_mode: str = "quality"
    frame_rate: str = "source"
    refetch_timeout: int = 600

    @classmethod
    def from_env(cls, path: Path | None = None):
        settings = cls()
        for spec in FIELDS.values():
            if spec.environment in os.environ:
                setattr(
                    settings, spec.attribute, spec.parse(os.environ[spec.environment])
                )
        if path is not None:
            for name, value in read_overrides(path).items():
                setattr(
                    settings, FIELDS[name].attribute, FIELDS[name].parse(str(value))
                )
        return settings


@dataclass(frozen=True)
class SettingField:
    attribute: str
    environment: str
    kind: type = str
    choices: tuple = ()
    minimum: float | None = None
    maximum: float | None = None
    unit: int = 1

    @property
    def options(self):
        if self.kind is bool:
            return "on, off"
        if self.choices:
            return ", ".join(map(str, self.choices))
        if self.kind is int:
            return f"{int(self.minimum)} to {int(self.maximum)} (integer)"
        return f"{self.minimum:g} to {self.maximum:g}"

    def parse(self, value: str):
        text = value.strip().lower()
        try:
            if self.kind is bool:
                if text not in {"on", "off", "true", "false", "yes", "no", "1", "0"}:
                    raise ValueError
                parsed = text in {"on", "true", "yes", "1"}
            else:
                parsed = self.kind(text)
                if self.choices and parsed not in self.choices:
                    raise ValueError
                if self.kind in {int, float}:
                    if not math.isfinite(parsed):
                        raise ValueError
                    if self.minimum is not None and parsed < self.minimum:
                        raise ValueError
                    if self.maximum is not None and parsed > self.maximum:
                        raise ValueError
        except (ValueError, OverflowError):
            raise ValueError(f"Valid options: {self.options}.") from None
        return parsed * self.unit if self.unit != 1 else parsed

    def display(self, settings: Settings):
        value = getattr(settings, self.attribute)
        if self.kind is bool:
            return "on" if value else "off"
        if self.unit != 1:
            value //= self.unit
        return str(value)


FIELDS = {
    "quality": SettingField(
        "quality", "OUTPUT_HEIGHT", int, (360, 720, 1080, 1440, 2160)
    ),
    "compress": SettingField(
        "compression_mode", "COMPRESSION_MODE", choices=("quality", "size")
    ),
    "crf": SettingField("video_crf", "VIDEO_CRF", int, minimum=16, maximum=28),
    "preset": SettingField(
        "encoder_preset",
        "VIDEO_PRESET",
        choices=(
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
        ),
    ),
    "threads": SettingField(
        "encode_threads", "ENCODE_THREADS", int, minimum=1, maximum=16
    ),
    "fps": SettingField(
        "frame_rate",
        "FRAME_RATE",
        choices=("source", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60"),
    ),
    "target_size": SettingField(
        "target_mb", "TARGET_SIZE_MB", int, minimum=50, maximum=2000
    ),
    "max_size": SettingField(
        "output_max_mb", "MAX_OUTPUT_MB", int, minimum=50, maximum=2000
    ),
    "upload_limit": SettingField(
        "upload_mb", "UPLOAD_LIMIT_MB", int, minimum=1, maximum=2000
    ),
    "reencode": SettingField("auto_compress", "ALLOW_SIZE_REENCODE", bool),
    "subtitles": SettingField(
        "subtitle_mode", "SUBTITLE_MODE", choices=("burn", "mux", "none")
    ),
    "dub_fallback": SettingField("dub_fallback", "DUB_FALLBACK", bool),
    "channel": SettingField(
        "channel_id", "CHANNEL_ID", int, minimum=0, maximum=2**64 - 1
    ),
    "delete_triggers": SettingField("delete_trigger", "DELETE_COMMAND_TRIGGERS", bool),
    "status_interval": SettingField(
        "status_interval", "STATUS_INTERVAL", int, minimum=5, maximum=120
    ),
    "upload_delay": SettingField("delay", "UPLOAD_DELAY", int, minimum=1, maximum=300),
    "keep_downloads": SettingField("keep_downloads", "KEEP_DOWNLOADS", bool),
    "auto_resume": SettingField("auto_resume", "AUTO_RESUME", bool),
    "upload_mode": SettingField(
        "upload_mode", "UPLOAD_MODE", choices=("auto", "cloud", "multipart")
    ),
    "upload_timeout": SettingField(
        "upload_timeout", "UPLOAD_TIMEOUT", int, minimum=30, maximum=14400
    ),
    "upload_attempts": SettingField(
        "upload_attempts", "UPLOAD_ATTEMPTS", int, minimum=1, maximum=3
    ),
    "upload_fallback": SettingField("upload_fallback", "UPLOAD_FALLBACK", bool),
    "upload_check_timeout": SettingField(
        "upload_check_timeout", "UPLOAD_CHECK_TIMEOUT", int, minimum=30, maximum=300
    ),
    "operation_timeout": SettingField(
        "operation_timeout", "OPERATION_TIMEOUT", int, minimum=60, maximum=86400
    ),
    "resolve_timeout": SettingField(
        "resolve_timeout", "RESOLVE_TIMEOUT", int, minimum=10, maximum=300
    ),
    "refetch_timeout": SettingField(
        "refetch_timeout", "REFETCH_TIMEOUT", int, minimum=30, maximum=3600
    ),
    "request_interval": SettingField(
        "request_interval", "REQUEST_INTERVAL", float, minimum=0.1, maximum=10
    ),
    "cooldown": SettingField(
        "rate_limit_cooldown", "RATE_LIMIT_COOLDOWN", int, minimum=60, maximum=3600
    ),
    "rate_retries": SettingField(
        "rate_limit_retries", "RATE_LIMIT_RETRIES", int, minimum=0, maximum=5
    ),
    "attempts": SettingField("attempts", "EPISODE_ATTEMPTS", int, minimum=1, maximum=5),
    "max_failures": SettingField(
        "max_consecutive_failures",
        "MAX_CONSECUTIVE_FAILURES",
        int,
        minimum=1,
        maximum=100,
    ),
    "max_episodes": SettingField(
        "max_episodes", "MAX_EPISODES", int, minimum=1, maximum=10000
    ),
    "disk_gib": SettingField(
        "max_temp_bytes", "MAX_TEMP_GIB", int, minimum=1, maximum=1000, unit=1024**3
    ),
    "headless": SettingField("headless", "HEADLESS", bool),
}
ALIASES = {
    "compression": "compress",
    "height": "quality",
    "video_crf": "crf",
    "encoder_preset": "preset",
}


def read_overrides(path: Path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(name not in FIELDS for name in data):
        raise ValueError("settings.json contains unknown fields or is not an object.")
    return data


def save_setting(path: Path, current: Settings, name: str, value: str):
    if path.is_symlink() or path.with_suffix(".tmp").is_symlink():
        raise ValueError("Refusing a symlinked settings file.")
    spec = FIELDS[name]
    updated = replace(current, **{spec.attribute: spec.parse(value)})
    overrides = read_overrides(path)
    overrides[name] = spec.display(updated)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(overrides, file, indent=2, sort_keys=True)
        file.flush()
        os.fsync(file.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)
    return updated


async def settings_command(bot, ctx, name: str = "", value: str = ""):
    if not name:
        lines = [f"{key} = {spec.display(bot.cfg)}" for key, spec in FIELDS.items()]
        await ctx.send("```\n" + "\n".join(lines) + "\n```")
        return
    name = ALIASES.get(name.lower(), name.lower())
    if name not in FIELDS:
        await ctx.send("Unknown setting. Use .settings to list configurable fields.")
        return
    spec = FIELDS[name]
    if not value:
        await ctx.send(
            f"{name} = {spec.display(bot.cfg)} | Valid options: {spec.options}."
        )
        return
    if bot.busy():
        await ctx.send(
            "Wait for the current job or maintenance task to stop before changing settings."
        )
        return
    try:
        parsed = spec.parse(value)
    except ValueError as exc:
        await ctx.send(f"{name}: {exc}")
        return
    if name == "channel" and parsed:
        try:
            target = await bot.channel_for(parsed)
            bot.check_permissions(target)
        except Exception:
            await ctx.send(
                "Channel is unavailable or lacks upload permissions. Settings were not changed."
            )
            return
        if bot.busy():
            await ctx.send(
                "A job started during channel validation. Settings were not changed."
            )
            return
    try:
        updated = save_setting(
            bot.library.path.parent / "settings.json", bot.cfg, name, value
        )
    except ValueError as exc:
        await ctx.send(f"{name}: {exc}")
        return
    bot.cfg = updated
    text = f"{name} = {spec.display(updated)} | Saved."
    if name == "preset" and updated.encoder_preset in {"ultrafast", "superfast"}:
        text += " This preset needs more bits; prefer fast/veryfast for action scenes."
    if name == "compress" and updated.compression_mode == "size":
        text += " A tight size budget can reduce detail during action."
    if name in {"compress", "target_size"} and updated.compression_mode == "quality":
        text += (
            f" Preferred size: {max(0, updated.target_mb - 50)}-{updated.target_mb + 50} MB."
            " Advisory only: CRF controls quality; the target does not constrain bitrate or force a smaller file."
        )
    await ctx.send(text)
