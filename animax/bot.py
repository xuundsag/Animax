from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import shutil
import signal
import time
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from .commands import register_commands
from .context import CommandContext
from .diagnostics import dependency_report
from .job import Job
from .media import (
    UploadTooLarge,
    cached_video,
    check_upload_size,
    download,
    fit_whole_video,
    probe,
)
from .metadata import (
    TitleError,
    clean_title,
    episode_caption,
    fetch_anime_title,
    provisional_title,
    saved_anime_title,
    title_override,
)
from .rate_limits import Cooldown, RateLimitError, cooldown_seconds
from .resolver import Resolver, resolve_with_fallback
from .routing import LogContext, Router, category_id
from .scheduler import Entry, Scheduler
from .settings import SAFE_OUTPUT_MB, Settings
from .storage import Library
from .stream_media import is_video_attachment
from .stream_runtime import StreamRuntime
from .terminal import Terminal, nice_error, setup_logging
from .upload import JournalCommitFailure, UploadFailure, upload_episode

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("animax")


def validate_request(mal, language, start, end, maximum):
    if mal < 1 or start < 1 or end < start or end - start + 1 > maximum:
        raise commands.BadArgument(
            f"Use positive MAL/episode numbers and an inclusive range of at most {maximum} episodes."
        )
    if language not in {"dub", "sub"}:
        raise commands.BadArgument("Choose dub or sub.")


class MegaPlayBot(commands.Bot):
    def __init__(self, settings: Settings, library: Library, ui=None, token=""):
        super().__init__(
            command_prefix=".",
            strip_after_prefix=True,
            self_bot=True,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.cfg, self.library, self.ui = settings, library, ui or Terminal(quiet=True)
        self.token = token or os.getenv("DISCORD_TOKEN", "").strip()
        self.stream_token = os.getenv("STREAM_DISCORD_TOKEN", "").strip()
        self.stream_runtime = None
        self.stream_manager = None
        self.stream_starting = False
        self.activity_lock = asyncio.Lock()
        self.cdn_cooldown = Cooldown(ROOT / "cdn_cooldown.json")
        self.job = None
        self.reset_request = None
        self.reset_task = None
        self.refetch_task = None
        self.shutting_down = False
        self.startup_done = False
        self.title_lock = asyncio.Lock()
        self.title_retry_after = 0.0
        self.title_next_request = 0.0

        self.scheduler = Scheduler(self, ROOT)
        self.router = Router(self)
        register_commands(self)

    def busy(self):
        stream_session = self.stream_manager.session if self.stream_manager else None
        stream_active = bool(
            stream_session and stream_session.task and not stream_session.task.done()
        )
        return (
            self.scheduler.busy()
            or stream_active
            or self.stream_starting
            or any(
                task is not None and not task.done()
                for task in (self.reset_task, self.refetch_task)
            )
        )

    async def ensure_stream_runtime(self):
        if not self.stream_token or self.stream_token.startswith("YOUR_"):
            raise RuntimeError(
                "Set STREAM_DISCORD_TOKEN in .env to a separate streaming user account before using `.stream`."
            )
        if self.stream_token == self.token:
            raise RuntimeError(
                "STREAM_DISCORD_TOKEN must be a different account token from DISCORD_TOKEN."
            )
        if self.stream_runtime is None:
            command_user_id = str(self.user.id) if self.user else None
            self.ui.event(
                "STREAM | Preparing the Go Live sidecar (separate account).", "cyan"
            )
            self.stream_runtime = StreamRuntime(
                self.stream_token,
                self.cfg,
                ROOT,
                command_user_id=command_user_id,
                library=self.library,
                attachment_lookup=self.stream_attachment_for_record,
                ui=self.ui,
            )
        self.stream_manager = await self.stream_runtime.start()
        return self.stream_manager

    async def stream_attachment_for_record(self, record):
        """Fetch the stored Discord message with the main command-only account."""
        channel_id = str(record.get("channel_id") or "")
        message_id = str(record.get("message_id") or "")
        if not channel_id.isdecimal() or not message_id.isdecimal():
            raise RuntimeError(
                "anime.json contains an invalid upload channel/message ID."
            )
        try:
            channel = await self.channel_for(channel_id)
            message = await channel.fetch_message(int(message_id))
        except Exception as exc:
            raise RuntimeError(
                f"Could not fetch saved Discord message {message_id} from channel {channel_id} "
                f"({type(exc).__name__}: {str(exc)[:240]}). Check that the main account can read that channel."
            ) from exc

        attachments = list(getattr(message, "attachments", ()) or ())
        videos = [
            attachment
            for attachment in attachments
            if is_video_attachment(
                getattr(attachment, "filename", ""),
                getattr(attachment, "content_type", None),
            )
        ]
        if not videos:
            raise RuntimeError(
                f"Saved Discord message {message_id} has no recognizable video attachment."
            )
        attachment = max(videos, key=lambda item: int(getattr(item, "size", 0) or 0))
        url = str(getattr(attachment, "url", "") or "")
        if not url:
            raise RuntimeError(
                f"Discord returned no current attachment URL for saved message {message_id}."
            )
        self.ui.event(
            f"STREAM | fetched anime.json message={message_id} channel={channel_id} "
            f"file={getattr(attachment, 'filename', 'video')} "
            f"size={int(getattr(attachment, 'size', 0) or 0)} bytes",
            "cyan",
        )
        return {
            "url": url,
            "filename": str(getattr(attachment, "filename", "episode-video")),
            "content_type": getattr(attachment, "content_type", None),
            "size": int(getattr(attachment, "size", 0) or 0),
            "message_id": str(message.id),
            "channel_id": str(channel.id),
        }

    async def get_context(self, origin, *, cls=CommandContext):
        return await super().get_context(origin, cls=cls)

    async def on_command_completion(self, ctx):
        if isinstance(ctx, CommandContext):
            await ctx.cleanup_trigger()

    async def on_ready(self):
        self.ui.event(f"Connected as {self.user}", "bold green")
        if not self.startup_done:
            self.startup_done = True
            self.scheduler.configure()
            if self.cfg.auto_resume:
                records = [
                    dict(r, id=k)
                    for k, r in self.library.data["jobs"].items()
                    if str(r.get("account_id")) == str(self.user.id)
                    and r.get("status") in {"queued", "running", "interrupted"}
                ]
                for record in sorted(records, key=lambda r: r.get("created_at", "")):
                    try:
                        await self.resume_record(record)
                    except Exception as exc:
                        log.exception("Automatic resume failed for %s", record["id"])
                        self.ui.error(exc)

    async def on_message(self, message):
        if self.user and message.author.id == self.user.id:
            await self.process_commands(message)

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.UserInputError):
            name = ctx.command.name if ctx.command else "download"
            await ctx.send(f"Invalid arguments. Use `.help {name}`.")
        else:
            log.error(
                "Command failed", exc_info=(type(error), error, error.__traceback__)
            )
            code, text = nice_error(error)
            self.ui.error(error)
            await ctx.send(f"{code}: {text} Details: logs/bot.log")

    async def channel_for(self, channel_id):
        channel = self.get_channel(int(channel_id)) or await self.fetch_channel(
            int(channel_id)
        )
        if not hasattr(channel, "send"):
            raise commands.BadArgument("Destination does not support messages.")
        return channel

    def check_permissions(self, target):
        guild = getattr(target, "guild", None)
        if guild and guild.me:
            permissions = target.permissions_for(guild.me)
            send = (
                permissions.send_messages_in_threads
                if isinstance(target, discord.Thread)
                else permissions.send_messages
            )
            if not (
                permissions.view_channel
                and send
                and permissions.attach_files
                and permissions.read_message_history
            ):
                raise commands.BadArgument(
                    "The account needs View Channel, Read Message History, Send Messages and Attach Files in the destination."
                )

    async def resume_record(self, record, ctx=None):
        if self.scheduler.contains(record["id"]):
            return False
        fixed = (
            await self.channel_for(record["channel_id"])
            if record.get("routed", True)
            else None
        )
        anchor = fixed or await self.channel_for(
            record.get("anchor_channel_id", record["channel_id"])
        )
        origin = (
            ctx.channel
            if ctx is not None and not self.cfg.channel_id
            else await self.channel_for(
                self.cfg.channel_id
                or record.get(
                    "log_channel_id",
                    record.get("command_channel_id", record["channel_id"]),
                )
            )
        )
        self.check_permissions(origin)
        if fixed:
            self.check_permissions(fixed)
        job = Job(
            int(record["mal"]),
            record["language"],
            int(record["start"]),
            int(record["end"]),
            fixed.id if fixed else anchor.id,
            title=record.get("title", ""),
            worker_id=int(record.get("worker_id") or 0),
            id=record["id"],
            fallback_notice_sent=bool(record.get("fallback_notice_sent")),
        )
        force = bool(
            record.get("force")
            and record.get("status") == "queued"
            and not record.get("routed", True)
        )
        return await self.launch_job(
            LogContext(origin, ctx),
            anchor,
            job,
            force,
            fixed_target=fixed,
            notify=ctx is not None,
        )

    async def download_command(
        self,
        ctx,
        mal_id: int,
        language: str,
        start: int,
        end: int,
        mode="resume",
        channel_mention="",
    ):
        language = language.lower()
        validate_request(mal_id, language, start, end, self.cfg.max_episodes)
        if self.stream_starting or (
            self.stream_manager and self.stream_manager.session
        ):
            task = (
                self.stream_manager.session.task
                if self.stream_manager and self.stream_manager.session
                else None
            )
            if self.stream_starting or (task and not task.done()):
                await ctx.send(
                    "A Go Live stream is active. Wait for it to finish or use `.stop` before starting a download."
                )
                return
        if mode.lower() not in {"resume", "force"}:
            raise commands.BadArgument("Mode must be resume or force.")
        if not getattr(ctx, "guild", None):
            await ctx.send(
                "Run `.download` in a server and mention the upload channel on first use."
            )
            return
        anchor = (
            await self.channel_for(self.cfg.channel_id)
            if self.cfg.channel_id
            else ctx.channel
        )
        self.check_permissions(anchor)
        preferred_category = (
            category_id(anchor)
            if getattr(getattr(anchor, "guild", None), "id", None) == ctx.guild.id
            else category_id(ctx.channel)
        )
        saved = await self.router.saved_target(
            ctx.guild.id, mal_id, preferred_category=preferred_category
        )
        if saved is not None:
            upload_target = saved
        else:
            match = re.fullmatch(r"<#(\d+)>", str(channel_mention or ""))
            if not match:
                await ctx.send(
                    f"First download of MAL {mal_id} in this server: add the destination channel mention at the end, e.g. `.download {mal_id} {language} {start} {end} #anime-channel`."
                )
                return
            upload_target = await self.channel_for(match.group(1))
            try:
                self.router._validate_text_channel(upload_target, ctx.guild.id)
            except ValueError as exc:
                raise commands.BadArgument(str(exc)) from None
        self.check_permissions(upload_target)
        job = Job(mal_id, language, start, end, upload_target.id)
        await self.router.remember_target(upload_target, job, anchor)
        started = await self.launch_job(
            ctx,
            anchor,
            job,
            mode.lower() == "force",
            fixed_target=upload_target,
        )
        if started and isinstance(ctx, CommandContext):
            await ctx.cleanup_trigger()
        elif started and self.cfg.delete_trigger:
            trigger = getattr(ctx, "message", None)
            if trigger is not None:
                await self.safe_delete(trigger, "download trigger")

    async def launch_job(
        self, ctx, target, job, force, *, fixed_target=None, notify=True
    ):
        blocked = None
        accepted = False
        async with self.activity_lock:
            session = self.stream_manager.session if self.stream_manager else None
            stream_active = bool(session and session.task and not session.task.done())
            if self.stream_starting or stream_active:
                blocked = "A Go Live stream is active. Wait for it to finish or use `.stop` before starting a download."
            elif any(
                task is not None and not task.done()
                for task in (self.reset_task, self.refetch_task)
            ):
                blocked = "A history maintenance task is still running."
            elif fixed_target is None and (
                not getattr(target, "guild", None) or isinstance(target, discord.Thread)
            ):
                blocked = "Choose a server text log channel; a saved or manually mentioned upload channel is required."
            else:
                self.reset_request = None
                log_ctx = (
                    ctx if isinstance(ctx, LogContext) else LogContext(target, ctx)
                )
                accepted = self.scheduler.enqueue(
                    Entry(log_ctx, target, job, force, fixed_target)
                )
        if blocked:
            await ctx.send(blocked)
            return False
        if accepted:
            self.job = job
            if notify:
                acknowledgement = (
                    ctx.origin if isinstance(ctx, LogContext) and ctx.origin else ctx
                )
                try:
                    await acknowledgement.send(
                        f"Queued `{job.id}` | MAL {job.mal} {job.language} E{job.start}-{job.end} | Status: <#{log_ctx.channel.id}>"
                    )
                except Exception:
                    log.exception(
                        "Job %s is queued durably, but its acknowledgement failed",
                        job.id,
                    )
        return accepted

    async def resolve_job_title(self, job):
        # An interrupted upload must retain the caption identity used by its saved intent.
        if job.title:
            return clean_title(job.title), "saved job"
        try:
            override = title_override(job.mal, ROOT / "title_overrides.json")
        except TitleError:
            log.warning(
                "Ignoring invalid title overrides; continuing with cache/provider/ID",
                exc_info=True,
            )
            override = None
        if override:
            return override, "override"
        cached = saved_anime_title(self.library.data, job.mal)
        if cached:
            return cached, "anime.json"
        if self.title_lock.locked():
            self.phase(job, "Waiting title lookup")
        async with self.title_lock:
            if time.monotonic() >= self.title_retry_after:
                self.phase(job, "Looking up title")
                await asyncio.sleep(max(0, self.title_next_request - time.monotonic()))
                try:
                    title = clean_title(
                        await asyncio.wait_for(fetch_anime_title(job.mal), timeout=20)
                    )
                    return title, "provider"
                except (TitleError, asyncio.TimeoutError):
                    self.title_retry_after = time.monotonic() + 60
                    log.warning(
                        "TITLE_LOOKUP_UNAVAILABLE job=%s mal=%s; continuing with MAL identity (no pause)",
                        job.id,
                        job.mal,
                        exc_info=True,
                    )
                finally:
                    self.title_next_request = time.monotonic() + 1
        return f"MAL {job.mal}", "MAL ID fallback"

    async def run_managed_job(self, entry):
        job = entry.job
        job.current = job.start
        job.title, title_source = await self.resolve_job_title(job)
        self.library.job(
            job.id,
            title=job.title,
            title_source=title_source,
            title_provisional=provisional_title(job.title, job.mal),
        )
        log.info(
            "DOWNLOAD_TITLE job=%s mal=%s source=%s title=%s",
            job.id,
            job.mal,
            title_source,
            job.title,
        )
        if title_source == "MAL ID fallback":
            self.ui.event(
                f"Title service unavailable; continuing as MAL {job.mal}", "yellow"
            )
        self.phase(job, "Preparing upload channel")
        target = entry.fixed_target or await self.router.resolve(entry.anchor, job)
        if entry.fixed_target:
            target = await self.router.remember_target(target, job, entry.anchor)
        self.check_permissions(target)
        job.channel_id = target.id
        self.library.job(
            job.id, channel_id=str(target.id), routed=True, title=job.title
        )
        await self.run_job(
            entry.ctx, target, job, self.cfg.upload_mb * 1_000_000, entry.force
        )

    async def report_loop(self, message, job):
        previous = ""
        while True:
            await asyncio.sleep(self.cfg.status_interval)
            self.ui.update(job)
            text = job.episode_text()
            if text != previous:
                try:
                    await message.edit(content=text)
                except discord.NotFound:
                    return
                except discord.HTTPException:
                    log.warning("Status-message edit failed", exc_info=True)
                previous = text

    async def safe_delete(self, message, label):
        # Only delete messages authored by the logged-in account.
        author = getattr(message, "author", None)
        if author is None or self.user is None or author.id != self.user.id:
            log.warning(
                "Refused to delete %s: message ownership was not verified", label
            )
            return False
        try:
            await asyncio.wait_for(message.delete(), timeout=20)
            return True
        except discord.NotFound:
            return True
        except Exception:
            log.warning(
                "Could not delete %s; upload result is not changed",
                label,
                exc_info=True,
            )
            self.ui.event(
                f"ANX-CLEANUP · Could not delete {label}. Details in logs/bot.log.",
                "yellow",
            )
            return False

    async def edit_status(self, message, job):
        if not message:
            return False
        try:
            await asyncio.wait_for(message.edit(content=job.episode_text()), timeout=20)
            return True
        except Exception:
            log.warning("Could not update episode progress message", exc_info=True)
            return False

    async def stop_reporter(self, reporter):
        if reporter:
            reporter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reporter

    def delivery(self, job, target):
        try:
            return self.library.data["anime"][str(job.mal)]["episodes"][job.language][
                str(job.current)
            ]["deliveries"][f"{self.user.id}:{target.id}"]
        except KeyError:
            return {}

    async def new_progress(self, ctx, job, target):
        message = await ctx.send(job.episode_text())
        references = list(self.delivery(job, target).get("progress_messages", []))
        if getattr(message, "id", None):
            references.append(
                {"channel_id": str(ctx.channel.id), "message_id": str(message.id)}
            )
        previous = self.delivery(job, target)
        self.record_episode(
            job,
            target,
            "preparing",
            progress_messages=references,
            progress_retained=False,
            needs_review=bool(
                previous.get("needs_review") or previous.get("status") == "uploading"
            ),
        )
        return message

    async def clear_progress(self, job, target, current, ctx):
        if self.cfg.channel_id or job.worker_id:
            if not self.delivery(job, target).get("progress_retained"):
                job.output_size_bytes = self.delivery(job, target).get("size_bytes")
                self.phase(job, "Done")
                all_updated = True
                for reference in self.delivery(job, target).get(
                    "progress_messages", []
                ):
                    try:
                        retained = (
                            current
                            if current and str(current.id) == reference["message_id"]
                            else await (
                                await self.channel_for(reference["channel_id"])
                            ).fetch_message(int(reference["message_id"]))
                        )
                        if not await self.edit_status(retained, job):
                            all_updated = False
                    except Exception:
                        all_updated = False
                        log.warning("Could not finalize retained status", exc_info=True)
                self.record_episode(
                    job, target, "uploaded", progress_retained=all_updated
                )
            return
        pending = []
        references = self.delivery(job, target).get("progress_messages", [])
        for reference in references:
            try:
                if (
                    current
                    and str(getattr(current, "id", "")) == reference["message_id"]
                    and str(ctx.channel.id) == reference["channel_id"]
                ):
                    message = current
                else:
                    channel = await self.channel_for(reference["channel_id"])
                    message = await channel.fetch_message(int(reference["message_id"]))
                if not await self.safe_delete(message, "episode progress message"):
                    pending.append(reference)
            except discord.NotFound:
                pass
            except Exception:
                log.warning(
                    "Progress cleanup failed after successful upload", exc_info=True
                )
                pending.append(reference)
        # The accepted video message ID was committed before reaching this method.
        self.record_episode(job, target, "uploaded", progress_messages=pending)

    def phase(self, job, text):
        if job.phase != text:
            log.info(
                "EPISODE_STAGE job=%s mal=%s language=%s episode=%s stage=%s",
                job.id,
                job.mal,
                job.language,
                job.current,
                text,
            )
            job.stage_started = time.monotonic()
        job.phase = text
        self.ui.update(job)

    async def wait_for_cdn(self, ctx, job):
        if self.cdn_cooldown.remaining() <= 0:
            return
        job.cooldown_until = self.cdn_cooldown.until
        self.phase(job, "CDN cooldown: no requests")
        self.ui.event(
            f"ANX-CDN429 · Waiting {math.ceil(self.cdn_cooldown.remaining())}s. This pause survives restarts.",
            "yellow",
        )
        while self.cdn_cooldown.remaining() > 0:
            await asyncio.sleep(min(30, self.cdn_cooldown.remaining()))
        job.cooldown_until = 0

    def record_episode(self, job, target, status, **kwargs):
        kwargs.setdefault("actual_language", job.actual_language or job.language)
        kwargs.setdefault("requested_language", job.language)
        kwargs.setdefault("fallback_reason", job.fallback_reason or None)
        self.library.record(
            job.mal,
            job.title,
            job.language,
            job.current,
            self.user.id,
            target.id,
            status,
            **kwargs,
        )

    def video_caption(self, job):
        return episode_caption(job.title, job.current)

    async def announce_fallback(self, ctx, job, episode, reason):
        if job.fallback_notice_sent:
            return
        cause = (
            "Dub captions unavailable"
            if reason == "dub captions unavailable"
            else "Dub unavailable"
        )
        message = (
            f"{job.title} E{episode}: {cause}; falling back to sub without captions."
        )
        job.fallback_notice_sent = True
        with contextlib.suppress(Exception):
            await ctx.send(message)
        with contextlib.suppress(OSError):
            self.library.job(job.id, fallback_notice_sent=True)

    def preserve_log(self, work, job):
        source = work / "process.log"
        destination = (
            ROOT / "logs" / f"episode-{job.mal}-{job.language}-{job.current}.log"
        )
        destination.parent.mkdir(exist_ok=True)
        if source.exists():
            # Bound retained subprocess logs; the latest 2 MiB contain the recent error.
            with source.open("rb") as file:
                file.seek(max(0, source.stat().st_size - 2 * 1024**2))
                destination.write_bytes(file.read())
            destination.chmod(0o600)
        return str(destination.relative_to(ROOT)) if source.exists() else "logs/bot.log"

    async def run_job(self, ctx, target, job, limit, force):
        cfg = self.cfg
        limit = min(
            limit,
            cfg.output_max_mb * 1_000_000,
            SAFE_OUTPUT_MB * 1_000_000,
        )
        reporter = None
        message = None
        status = "failed"
        work = None
        consecutive = 0
        try:
            self.library.job(job.id, status="running")
            cached_title = self.library.data["anime"].get(str(job.mal), {}).get("title")
            try:
                if not job.title:
                    job.title = await fetch_anime_title(
                        job.mal, ROOT / "title_overrides.json"
                    )
            except TitleError:
                if cached_title and not cached_title.startswith("MAL "):
                    job.title = clean_title(cached_title)
                else:
                    raise
            self.library.job(job.id, title=job.title)
            for ep in range(job.start, job.end + 1):
                existing = (
                    self.library.data["anime"]
                    .get(str(job.mal), {})
                    .get("episodes", {})
                    .get(job.language, {})
                    .get(str(ep), {})
                    .get("deliveries", {})
                    .get(f"{self.user.id}:{target.id}")
                )
                if existing is None:
                    self.library.record(
                        job.mal,
                        job.title,
                        job.language,
                        ep,
                        self.user.id,
                        target.id,
                        "pending",
                        save=False,
                    )
            self.library.save()
            self.ui.start(job.title, key=job.id)
            for episode in range(job.start, job.end + 1):
                job.current = episode
                job.output_size_bytes = None
                job.progress = {"fraction": 0}
                job.error = ""
                job.notice = ""
                job.actual_language = job.language
                job.fallback_reason = ""
                if not force and self.library.completed(
                    job.mal, job.language, episode, self.user.id, target.id
                ):
                    old = self.delivery(job, target)
                    job.actual_language = old.get("actual_language", job.language)
                    job.fallback_reason = old.get("fallback_reason") or ""
                    job.notice = old.get("subtitle_note") or ""
                    # Recover cleanup after a crash between the accepted-ID commit and deletion.
                    if self.delivery(job, target).get("progress_messages"):
                        try:
                            await self.clear_progress(job, target, None, ctx)
                        except Exception:
                            log.warning(
                                "Deferred progress cleanup failed", exc_info=True
                            )
                    job.skipped += 1
                    continue
                work = (
                    ROOT
                    / "downloads"
                    / f"{self.user.id}-{target.id}-{job.mal}-{job.language}-{episode}"
                )
                work.mkdir(parents=True, exist_ok=True, mode=0o700)
                upload_report = (
                    ROOT / "logs" / f"upload-{job.mal}-{job.language}-{episode}.json"
                )
                if job.worker_id:
                    old_report = upload_report
                    upload_report = (
                        ROOT
                        / "logs"
                        / "uploads"
                        / f"{self.user.id}-{target.id}"
                        / old_report.name
                    )
                    upload_report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if old_report.exists() and not upload_report.exists():
                        with contextlib.suppress(OSError, ValueError):
                            legacy = json.loads(old_report.read_text())
                            if str(legacy.get("channel_id")) == str(target.id):
                                shutil.copyfile(old_report, upload_report)
                                upload_report.chmod(0o600)
                delivered = False
                upload_started = False
                message = None
                reporter = None
                previous = dict(self.delivery(job, target))
                try:
                    self.phase(job, "Preparing")
                    message = await self.new_progress(ctx, job, target)
                    reporter = asyncio.create_task(self.report_loop(message, job))
                    pending_upload = bool(
                        previous.get("needs_review")
                        or previous.get("status") == "uploading"
                    )
                    cached_language = job.language
                    marker = work / "ready.json"
                    ready = {}
                    if marker.exists():
                        with contextlib.suppress(ValueError, OSError):
                            ready = json.loads(marker.read_text())
                    if (
                        job.language == "dub"
                        and cfg.dub_fallback
                        and ready.get("requested_language") == "dub"
                        and ready.get("language") == "sub"
                        and ready.get("fallback_reason")
                    ):
                        cached_language = "sub"
                    cached = await cached_video(
                        work,
                        cached_language,
                        cfg.quality,
                        cfg.subtitle_mode,
                        cfg.subtitle_fallback,
                        encoder_preset=cfg.encoder_preset,
                        target_mb=cfg.target_mb,
                        video_crf=cfg.video_crf,
                        compression_mode=cfg.compression_mode,
                        compress_all=cfg.compress_all,
                        lossless=cfg.lossless,
                        caption_font=cfg.caption_font,
                        frame_rate=cfg.frame_rate,
                        pending_upload=pending_upload,
                    )
                    if pending_upload and not cached:
                        raise UploadFailure(
                            "A previous upload is unresolved and its matching prepared cache is unavailable. Restore the cache/settings or review the destination before another encode/send.",
                            stage="review",
                            outcome_unknown=True,
                        )
                    if cached:
                        job.actual_language = cached_language
                        job.fallback_reason = ready.get("fallback_reason") or ""
                        if (
                            job.language == "dub"
                            and job.actual_language == "sub"
                            and job.fallback_reason
                        ):
                            await self.announce_fallback(
                                ctx, job, episode, job.fallback_reason
                            )
                    source, note = cached if cached else (None, None)
                    if cached:
                        job.progress = {"fraction": 1, "estimated": False}
                        self.phase(job, "Using verified local episode")
                    attempt = 0
                    rate_hits = 0
                    while source is None:
                        try:
                            async with self.scheduler.resources.lease(
                                "transfer", job
                            ) as transfer_lease:
                                await self.wait_for_cdn(ctx, job)
                                self.phase(
                                    job,
                                    f"Resolving · attempt {attempt + 1}/{cfg.attempts}",
                                )
                                self.record_episode(job, target, "downloading")
                                async with Resolver(
                                    cfg.resolve_timeout, cfg.headless
                                ) as resolver:
                                    async with resolve_with_fallback(
                                        resolver,
                                        job.mal,
                                        job.language,
                                        episode,
                                        cfg.dub_fallback,
                                    ) as resolved:
                                        stream, job.actual_language, reason = resolved
                                        job.fallback_reason = reason or ""
                                        self.record_episode(job, target, "resolved")
                                        if reason:
                                            await self.announce_fallback(
                                                ctx, job, episode, reason
                                            )
                                        transfer = await stream.detach(
                                            job.actual_language,
                                            with_captions=not reason
                                            and (
                                                job.actual_language == "dub"
                                                or cfg.subtitle_mode != "none"
                                            ),
                                        )
                                self.phase(job, "Downloading")

                                def progress(data):
                                    job.progress = data
                                    self.ui.update(job)

                                source, note = await download(
                                    transfer,
                                    work,
                                    job.actual_language,
                                    quality=cfg.quality,
                                    subtitle_mode="none"
                                    if job.fallback_reason
                                    else cfg.subtitle_mode,
                                    max_bytes=cfg.max_temp_bytes,
                                    timeout=cfg.operation_timeout,
                                    prepare_timeout=cfg.prepare_timeout,
                                    request_interval=max(
                                        cfg.request_interval, self.cdn_cooldown.interval
                                    ),
                                    on_progress=progress,
                                    on_phase=lambda value: self.phase(job, value),
                                    subtitle_fallback=False,
                                    encoder_preset=cfg.encoder_preset,
                                    upload_limit=limit,
                                    encode_threads=self.scheduler.resources.plan.encoder_threads,
                                    target_mb=cfg.target_mb,
                                    video_crf=cfg.video_crf,
                                    compression_mode=cfg.compression_mode,
                                    compress_all=cfg.compress_all,
                                    lossless=cfg.lossless,
                                    caption_font=cfg.caption_font,
                                    frame_rate=cfg.frame_rate,
                                    resources=self.scheduler.resources,
                                    job=job,
                                    transfer_lease=transfer_lease,
                                )
                                marker = work / "ready.json"
                                ready = json.loads(marker.read_text())
                                ready.update(
                                    requested_language=job.language,
                                    fallback_reason=job.fallback_reason,
                                )
                                temporary = marker.with_suffix(".tmp")
                                temporary.write_text(json.dumps(ready))
                                temporary.replace(marker)
                        except RateLimitError as exc:
                            rate_hits += 1
                            self.cdn_cooldown.slow_down(cfg.request_interval)
                            self.cdn_cooldown.defer(
                                cooldown_seconds(
                                    rate_hits, cfg.rate_limit_cooldown, exc.retry_after
                                )
                            )
                            job.cooldown_until = self.cdn_cooldown.until
                            self.preserve_log(work, job)
                            if rate_hits > cfg.rate_limit_retries:
                                raise
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            attempt += 1
                            log.exception(
                                "Episode %s attempt %s failed", episode, attempt
                            )
                            self.ui.error(exc, self.preserve_log(work, job))
                            if attempt >= cfg.attempts:
                                raise
                            self.phase(
                                job,
                                f"Retrying same episode in {min(5 * 2 ** (attempt - 1), 30)}s",
                            )
                            await asyncio.sleep(min(5 * 2 ** (attempt - 1), 30))
                    job.progress["fraction"] = 1
                    if source.stat().st_size > limit and not cfg.lossless:
                        async with self.scheduler.resources.lease("encode", job):
                            self.phase(job, "Enhancing")
                            source = await fit_whole_video(
                                source,
                                limit,
                                timeout=cfg.operation_timeout,
                                max_bytes=cfg.max_temp_bytes,
                                disk_root=self.scheduler.resources.disk_root,
                                encode_threads=self.scheduler.resources.plan.encoder_threads,
                            )
                            marker = work / "ready.json"
                            if marker.exists():
                                ready = json.loads(marker.read_text())
                                ready["file"] = source.name
                                temporary = marker.with_suffix(".tmp")
                                temporary.write_text(json.dumps(ready))
                                temporary.replace(marker)
                    self.phase(job, "Checking video")
                    check_upload_size(source, limit)
                    info = await probe(source)
                    log.info(
                        "VIDEO_READY mal=%s episode=%s bytes=%s duration=%s format=%s",
                        job.mal,
                        episode,
                        source.stat().st_size,
                        info["format"]["duration"],
                        info["format"].get("format_name", "unknown"),
                    )
                    if cfg.compression_mode == "quality":
                        size_mb = source.stat().st_size / 1_000_000
                        low, high = max(0, cfg.target_mb - 50), cfg.target_mb + 50
                        relation = (
                            "below"
                            if size_mb < low
                            else "above"
                            if size_mb > high
                            else "within"
                        )
                        log.info(
                            "SIZE_PREFERENCE actual_mb=%.1f preferred_mb=%s-%s relation=%s policy=advisory; no size-fitting re-encode",
                            size_mb,
                            low,
                            high,
                            relation,
                        )
                    if note:
                        job.notice = note
                        self.ui.event(note, "yellow")
                    async with self.scheduler.resources.lease("upload", job):
                        self.check_permissions(target)
                        self.phase(job, "Uploading")
                        await self.edit_status(message, job)
                        self.record_episode(
                            job, target, "uploading", subtitle_note=note
                        )
                        upload_started = True
                        previous_upload = self.library.previous_message(
                            job.mal, episode, self.user.id, target.id, job.language
                        )
                        reference = None
                        if previous_upload:
                            reference = discord.MessageReference(
                                message_id=int(previous_upload["message_id"]),
                                channel_id=target.id,
                                guild_id=getattr(
                                    getattr(target, "guild", None), "id", None
                                ),
                                fail_if_not_exists=False,
                            )
                        sent = await upload_episode(
                            target,
                            source,
                            self.video_caption(job),
                            filename=f"MAL-{job.mal}_EP-{episode:04d}_{job.actual_language}.mp4",
                            report_path=upload_report,
                            timeout=cfg.upload_timeout,
                            mode=cfg.upload_mode,
                            on_phase=lambda value: self.phase(job, value),
                            reference=reference,
                            author_id=self.user.id,
                            state_path=work / "upload-state.json",
                            attempts=cfg.upload_attempts,
                            fallback=cfg.upload_fallback,
                            verify_timeout=cfg.upload_check_timeout,
                            on_event=lambda text: self.ui.event(
                                f"E{episode} | {text}", "yellow"
                            ),
                            recover_existing=pending_upload,
                            new_delivery=bool(
                                force
                                and previous.get("status") == "uploaded"
                                and not pending_upload
                            ),
                        )
                        try:
                            # Commit the accepted ID before deleting progress or cached video.
                            self.record_episode(
                                job,
                                target,
                                "uploaded",
                                message_id=str(sent.id),
                                message_ids=[str(sent.id)],
                                jump_url=getattr(sent, "jump_url", None),
                                size_bytes=source.stat().st_size,
                                duration_seconds=float(info["format"]["duration"]),
                                filename=source.name,
                                needs_review=False,
                                guild_id=getattr(
                                    getattr(target, "guild", None), "id", None
                                ),
                                reply_to_message_id=str(reference.message_id)
                                if reference
                                else None,
                                caption_update_pending=False,
                            )
                        except Exception as exc:
                            log.critical(
                                "UPLOAD ACCEPTED BUT JOURNAL FAILED: channel=%s message=%s",
                                target.id,
                                sent.id,
                                exc_info=True,
                            )
                            raise JournalCommitFailure(
                                f"Discord accepted message {sent.id}, but anime.json could not be saved. Do not resend blindly; repair the journal first.",
                                stage="journal",
                                outcome_unknown=True,
                            ) from exc
                    delivered = True
                    upload_started = False
                    job.sent += 1
                    consecutive = 0
                    job.output_size_bytes = self.delivery(job, target)["size_bytes"]
                    self.phase(job, "Done")
                    await self.stop_reporter(reporter)
                    reporter = None
                    await self.edit_status(message, job)
                    # Cleanup failures never reclassify an accepted/journaled video as failed.
                    try:
                        await self.clear_progress(job, target, message, ctx)
                    except Exception:
                        log.warning(
                            "Uploaded message %s is safe, but progress cleanup could not be saved",
                            sent.id,
                            exc_info=True,
                        )
                    message = None
                    try:
                        self.cdn_cooldown.relax(cfg.request_interval)
                        if not cfg.keep_downloads:
                            shutil.rmtree(work)
                    except OSError:
                        log.warning(
                            "Post-upload housekeeping failed; the video remains uploaded",
                            exc_info=True,
                        )
                    self.ui.event(
                        f"EP {episode} delivered · message {sent.id}", "bold green"
                    )
                    await asyncio.sleep(
                        max(cfg.delay, getattr(target, "slowmode_delay", 0))
                    )
                except asyncio.CancelledError:
                    # A cancel during final message posting may leave an accepted message
                    # without a reply. Do not auto-resume such an episode after restart.
                    if upload_started and upload_report.exists():
                        with contextlib.suppress(OSError, ValueError):
                            if json.loads(upload_report.read_text()).get(
                                "outcome_unknown"
                            ):
                                self.record_episode(
                                    job, target, "needs_review", needs_review=True
                                )
                                status = "needs_review"
                    raise
                except Exception as exc:
                    log.exception("Episode %s stopped at %s", episode, job.phase)
                    code, text = nice_error(exc)
                    media_path = self.preserve_log(work, job)
                    path = (
                        str(upload_report.relative_to(ROOT))
                        if isinstance(exc, UploadFailure)
                        else media_path
                    )
                    review = isinstance(exc, UploadFailure) and exc.outcome_unknown
                    if review:
                        status = "needs_review"
                    with contextlib.suppress(OSError):
                        self.record_episode(
                            job,
                            target,
                            "needs_review" if review else "failed",
                            needs_review=review,
                            error={"code": code, "message": text, "log": path},
                        )
                    self.ui.error(exc, path)
                    job.failed += 1
                    consecutive += 1
                    if isinstance(exc, UploadFailure):
                        self.phase(job, "Paused")
                        job.error = ""
                    else:
                        self.phase(job, "Failed")
                        job.error = f"{code}: {text}\nDetails : `{path}`"
                    if not await self.edit_status(message, job):
                        with contextlib.suppress(Exception):
                            await ctx.send(job.episode_text())
                    if (
                        isinstance(
                            exc,
                            (
                                RateLimitError,
                                discord.HTTPException,
                                UploadTooLarge,
                                UploadFailure,
                            ),
                        )
                        or consecutive >= cfg.max_consecutive_failures
                    ):
                        break
                finally:
                    await self.stop_reporter(reporter)
                    reporter = None
                    if delivered:
                        message = None
            else:
                status = "finished" if job.failed == 0 else "finished_with_errors"
                self.phase(
                    job,
                    "Finished"
                    if not job.failed
                    else "Finished with missing episodes; use .retry",
                )
        except asyncio.CancelledError:
            if status != "needs_review":
                status = "interrupted" if self.shutting_down else "cancelled"
            self.phase(
                job,
                "Needs review"
                if status == "needs_review"
                else "Interrupted"
                if self.shutting_down
                else "Cancelled",
            )
            if message and not self.shutting_down:
                await self.edit_status(message, job)
        except Exception as exc:
            log.exception("Job stopped")
            self.ui.error(exc)
            self.phase(job, "Failed")
            code, text = nice_error(exc)
            job.error = f"{code}: {text}\nDetails : `logs/bot.log`"
            if message:
                await self.edit_status(message, job)
            else:
                with contextlib.suppress(Exception):
                    await ctx.send(job.episode_text())
        finally:
            try:
                self.library.job(
                    job.id,
                    status=status,
                    current=job.current,
                    sent=job.sent,
                    failed=job.failed,
                    skipped=job.skipped,
                    title=job.title,
                )
            except OSError:
                log.critical("Could not persist final job state", exc_info=True)
            await self.stop_reporter(reporter)
            self.ui.finish(job.id)

    async def close(self):
        self.shutting_down = True
        await self.scheduler.close()
        if self.refetch_task and not self.refetch_task.done():
            self.refetch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.refetch_task
        if self.reset_task and not self.reset_task.done():
            await asyncio.shield(self.reset_task)
        if self.stream_runtime:
            await self.stream_runtime.close()
        self.ui.finish()
        await super().close()


class InstanceLock:
    def __init__(self, path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+")
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                self.file.seek(0)
                self.file.write(" ")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            raise RuntimeError(
                "Another ANIMAX instance is already running. Stop it before starting another."
            ) from None
        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()


async def run_client(bot, token):
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, lambda: asyncio.create_task(bot.close()))
    async with bot:
        await bot.start(token, reconnect=True)


def main():
    load_dotenv(ROOT / ".env")
    setup_logging(ROOT)
    ui = Terminal()
    try:
        dependency_report()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token or token.startswith("YOUR_"):
            raise ValueError(
                "Set the main user's DISCORD_TOKEN locally in .env before starting. Do not share it."
            )
        cfg = Settings.from_env(ROOT / "settings.json")
        with InstanceLock(ROOT / ".animax.lock"):
            from .reset import recover_resets

            recover_resets(ROOT)
            library = Library(ROOT / "anime.json")
            bot = MegaPlayBot(cfg, library, ui, token)
            asyncio.run(run_client(bot, token))
    except (ValueError, discord.LoginFailure) as exc:
        log.exception("Configuration or login failed")
        ui.error(exc)
        raise SystemExit(78)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.exception("Startup stopped")
        ui.error(exc)
        raise SystemExit(1)
