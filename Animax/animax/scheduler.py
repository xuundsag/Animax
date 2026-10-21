"""Persistent serial job queue; only one download job can run at a time."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass

from .resources import Resources

log = logging.getLogger("animax.scheduler")


@dataclass
class Entry:
    ctx: object
    anchor: object
    job: object
    force: bool
    fixed_target: object = None


class Scheduler:
    def __init__(self, bot, root):
        self.bot, self.root = bot, root
        self.queue = deque()
        self.active = {}
        self.tasks = {}
        self.dispatcher = None
        self.event = asyncio.Event()
        self.closing = False
        self.blocked = ""
        self.resources = Resources(root, bot.cfg, bot.phase, bot.cdn_cooldown)

    def busy(self):
        return bool(self.queue or self.active) or any(
            gate.locked() for gate in self.resources.gates.values()
        )

    def contains(self, job_id):
        return any(e.job.id == job_id for e in self.queue) or any(
            e.job.id == job_id for e in self.active.values()
        )

    def configure(self):
        if not self.busy():
            self.resources = Resources(
                self.root, self.bot.cfg, self.bot.phase, self.bot.cdn_cooldown
            )
            if self.bot.startup_done:
                self.bot.ui.event(f"Serial download plan: {self.resources.plan.data()}")

    def enqueue(self, entry):
        if self.closing or self.contains(entry.job.id):
            return False
        self.configure()
        job = entry.job
        self.bot.library.job(
            job.id,
            account_id=str(self.bot.user.id),
            mal=job.mal,
            language=job.language,
            start=job.start,
            end=job.end,
            title=job.title,
            status="queued",
            channel_id=str(
                entry.fixed_target.id if entry.fixed_target else entry.anchor.id
            ),
            log_channel_id=str(entry.ctx.channel.id),
            command_channel_id=str(entry.ctx.channel.id),
            anchor_channel_id=str(entry.anchor.id),
            force=entry.force,
            route_version=2,
            routed=entry.fixed_target is not None,
            fallback_notice_sent=job.fallback_notice_sent,
        )
        self.queue.append(entry)
        self.event.set()
        if self.dispatcher is None or self.dispatcher.done():
            self.dispatcher = asyncio.create_task(self._dispatch(), name="ANIMAX queue")
        return True

    async def _dispatch(self):
        try:
            while not self.closing and (self.queue or self.active):
                self.event.clear()
                self.blocked = ""
                while self.queue and len(self.active) < self.resources.plan.active_jobs:
                    occupied_anime = {e.job.mal for e in self.active.values()}
                    entry = next(
                        (e for e in self.queue if e.job.mal not in occupied_anime), None
                    )
                    if entry is None:
                        self.blocked = "Waiting for the active job of this anime"
                        break
                    try:
                        self.blocked = self.resources.admission_reason()
                    except OSError:
                        self.blocked = (
                            "Cannot inspect available resources; admission paused"
                        )
                    if self.blocked:
                        break
                    slot = 1  # Fixed single-job slot; no worker pool in v2.4.4.
                    try:
                        self.bot.library.job(entry.job.id, status="running")
                    except OSError:
                        self.blocked = "Cannot save the job journal; admission paused"
                        log.exception(self.blocked)
                        break
                    self.queue.remove(entry)
                    self.active[slot] = entry
                    self.tasks[slot] = asyncio.create_task(
                        self._run(slot, entry), name="ANIMAX download job"
                    )
                    self.tasks[slot].add_done_callback(
                        lambda task, s=slot, e=entry: self._finished(s, e, task)
                    )
                if not self.queue and not self.active:
                    break
                try:
                    await asyncio.wait_for(self.event.wait(), 5)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.dispatcher = None

    def _finished(self, slot, entry, task):
        if self.tasks.get(slot) is task:
            self.active.pop(slot, None)
            self.tasks.pop(slot, None)
            if task.cancelled():
                with contextlib.suppress(OSError):
                    self.bot.library.job(
                        entry.job.id,
                        status="interrupted" if self.bot.shutting_down else "cancelled",
                    )
            self.event.set()
        if not task.cancelled() and task.exception():
            log.error("Download job cleanup failed", exc_info=task.exception())

    async def _run(self, slot, entry):
        try:
            await self.bot.run_managed_job(entry)
        except asyncio.CancelledError:
            with contextlib.suppress(OSError):
                self.bot.library.job(
                    entry.job.id,
                    status="interrupted" if self.bot.shutting_down else "cancelled",
                )
            raise
        except Exception as exc:
            log.exception("Download job setup failed before its episode pipeline")
            with contextlib.suppress(OSError):
                self.bot.library.job(
                    entry.job.id, status="failed", title=entry.job.title
                )
            entry.job.phase = "Failed"
            entry.job.error = (
                str(exc)[:260]
                if isinstance(exc, ValueError)
                else "Download job setup failed; see logs/bot.log."
            )
            with contextlib.suppress(Exception):
                await entry.ctx.send(entry.job.episode_text())
            self.bot.ui.event(
                f"Job {entry.job.id} setup failed. See logs/bot.log", "red"
            )
        finally:
            self.bot.ui.finish(entry.job.id)
            self.active.pop(slot, None)
            self.tasks.pop(slot, None)
            self.event.set()

    def status_text(self):
        lines = [
            e.job.episode_text() + f" | `{e.job.id}`"
            for _, e in sorted(self.active.items())
        ]
        lines.append(
            f"Queued: {len(self.queue)} download jobs | Concurrent download jobs: 1 (fixed) | Shared encode threads: {self.resources.plan.encoder_threads}"
        )
        if self.blocked:
            lines.append(self.blocked)
        for entry in list(self.queue)[:8]:
            lines.append(
                f"`{entry.job.id}` | MAL {entry.job.mal} {entry.job.language} E{entry.job.start}-{entry.job.end} | Queued"
            )
        return "\n".join(lines)[:1900]

    async def cancel(self, selector=""):
        selector = selector.strip().lower()
        if not selector:
            if len(self.active) == 1:
                selector = next(iter(self.active.values())).job.id
            elif not self.active and len(self.queue) == 1:
                selector = self.queue[0].job.id
            else:
                return "Use `.cancel JOB_ID` or `.cancel all`."
        selected = [
            (slot, entry)
            for slot, entry in self.active.items()
            if selector in {"all", entry.job.id}
        ]
        queued = [e for e in self.queue if selector in {"all", e.job.id}]
        for entry in queued:
            self.bot.library.job(entry.job.id, status="cancelled")
            self.queue.remove(entry)
        tasks = [self.tasks[slot] for slot, _ in selected]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.event.set()
        return f"Cancelled {len(tasks)} active and {len(queued)} queued download jobs. History and cached videos were kept."

    async def close(self):
        self.closing = True
        if self.dispatcher:
            self.dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.dispatcher
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.queue.clear()
