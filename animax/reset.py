from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("animax.reset")
RESET_PATHS = (
    "anime.json",
    "anime.json.bak",
    "anime.json.tmp",
    "state.json",
    "state.json.bak",
    "state.json.tmp",
    "downloads",
)


@dataclass(frozen=True)
class ResetRequest:
    account_id: int
    channel_id: int
    token: str
    expires: float


def write_manifest(folder: Path, data: dict):
    temporary = folder / "manifest.tmp"
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(folder / "manifest.json")


def remove_path(path: Path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def recover_resets(root: Path):
    for folder in root.glob(".reset-pending-*"):
        if folder.is_symlink() or not folder.is_dir():
            raise RuntimeError("Refusing an invalid reset recovery directory.")
        marker = folder / "manifest.json"
        if not marker.exists():
            if not any(folder.iterdir()):
                folder.rmdir()
                continue
            raise RuntimeError(
                "Reset recovery manifest is missing; inspect the reset directory before starting."
            )
        manifest = json.loads(marker.read_text())
        if not set(manifest["paths"]).issubset(RESET_PATHS):
            raise RuntimeError("Reset recovery manifest contains unexpected paths.")
        if manifest["phase"] != "committed":
            for name in manifest["paths"]:
                staged = folder / name
                if staged.exists() or staged.is_symlink():
                    remove_path(root / name)
                    staged.replace(root / name)
        shutil.rmtree(folder)


def reset_history(root: Path, library):
    recover_resets(root)
    paths = [name for name in RESET_PATHS if (root / name).exists()]
    for name in RESET_PATHS:
        if (root / name).is_symlink():
            raise RuntimeError(f"Refusing to reset a symlink: {name}")
    folder = root / f".reset-pending-{secrets.token_hex(8)}"
    folder.mkdir(mode=0o700)
    manifest = {"phase": "staging", "paths": paths}
    write_manifest(folder, manifest)
    old_data = library.data
    try:
        for name in paths:
            (root / name).replace(folder / name)
        library.data = {"version": 1, "anime": {}, "jobs": {}}
        library.save()
        manifest["phase"] = "committed"
        write_manifest(folder, manifest)
    except BaseException:
        library.data = old_data
        recover_resets(root)
        raise
    # A committed reset must not restore history if deleting large caches fails.
    try:
        shutil.rmtree(folder)
        return True
    except OSError:
        log.warning(
            "Reset committed; staged cache cleanup will retry at startup", exc_info=True
        )
        return False


async def handle_reset(bot, ctx, action: str, token: str):
    if bot.reset_task and not bot.reset_task.done():
        await ctx.send("Reset is already running.")
        return
    if bot.busy():
        await ctx.send(
            "Use .cancel all and wait for all jobs to stop before resetting."
        )
        return
    action = action.lower()
    if action == "cancel":
        bot.reset_request = None
        await ctx.send("Reset cancelled.")
        return
    if not action:
        request = ResetRequest(
            bot.user.id, ctx.channel.id, secrets.token_hex(3), time.monotonic() + 60
        )
        bot.reset_request = request
        await ctx.send(
            "Clear all local anime history, saved jobs and downloaded videos? "
            f"Confirm here within 60 seconds: `.reset confirm {request.token}`. "
            "Settings, existing Discord messages and cooldowns stay. Use `.reset cancel` to abort."
        )
        return
    request = bot.reset_request
    if (
        action != "confirm"
        or request is None
        or request.expires < time.monotonic()
        or request.account_id != bot.user.id
        or request.channel_id != ctx.channel.id
        or not secrets.compare_digest(request.token, token)
    ):
        await ctx.send("Confirmation is invalid or expired. Use .reset to start again.")
        return
    bot.reset_request = None
    root = bot.library.path.parent
    bot.reset_task = asyncio.create_task(
        asyncio.to_thread(reset_history, root, bot.library)
    )
    cleaned = await asyncio.shield(bot.reset_task)
    bot.job = None
    text = "Reset complete. The next download starts from scratch, including episode 1."
    if not cleaned:
        text += " Old cache cleanup will retry at startup."
    await ctx.send(text)
