"""Persistent job queue; bounded active workers with one job per anime at a time."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass

from .resources import Resources, apply_cpu_budget

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
                applied = apply_cpu_budget(self.resources.plan.encoder_threads)
                self.bot.ui.event(
                    f"Resource plan: {self.resources.plan.data()} | CPU affinity applied: {applied}"
                )

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
            worker_id=None,
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
                while (
                    self.queue and len(self.active) < self.resources.plan.active_workers
                ):
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
                    worker = next(
                        i
                        for i in range(1, self.resources.plan.active_workers + 1)
                        if i not in self.active
                    )
                    try:
                        self.bot.library.job(
                            entry.job.id, status="running", worker_id=worker
                        )
                    except OSError:
                        self.blocked = "Cannot save the job journal; admission paused"
                        log.exception(self.blocked)
                        break
                    self.queue.remove(entry)
                    entry.job.worker_id = worker
                    self.active[worker] = entry
                    self.tasks[worker] = asyncio.create_task(
                        self._run(worker, entry), name=f"ANIMAX W{worker}"
                    )
                    self.tasks[worker].add_done_callback(
                        lambda task, w=worker, e=entry: self._finished(w, e, task)
                    )
                if not self.queue and not self.active:
                    break
                try:
                    await asyncio.wait_for(self.event.wait(), 5)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.dispatcher = None

    def _finished(self, worker, entry, task):
        if self.tasks.get(worker) is task:
            self.active.pop(worker, None)
            self.tasks.pop(worker, None)
            if task.cancelled():
                with contextlib.suppress(OSError):
                    self.bot.library.job(
                        entry.job.id,
                        status="interrupted" if self.bot.shutting_down else "cancelled",
                    )
            self.event.set()
        if not task.cancelled() and task.exception():
            log.error("Worker cleanup failed", exc_info=task.exception())

    async def _run(self, worker, entry):
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
            log.exception(
                "Worker %s failed before starting its episode pipeline", worker
            )
            with contextlib.suppress(OSError):
                self.bot.library.job(
                    entry.job.id, status="failed", title=entry.job.title
                )
            entry.job.phase = "Failed"
            entry.job.error = (
                str(exc)[:260]
                if isinstance(exc, ValueError)
                else "Worker setup failed; see logs/bot.log."
            )
            with contextlib.suppress(Exception):
                await entry.ctx.send(entry.job.episode_text())
            self.bot.ui.event(
                f"W{worker} | Job {entry.job.id} setup failed. See logs/bot.log", "red"
            )
        finally:
            self.bot.ui.finish(entry.job.id)
            self.active.pop(worker, None)
            self.tasks.pop(worker, None)
            self.event.set()

    def status_text(self):
        lines = [
            e.job.episode_text() + f" | `{e.job.id}`"
            for _, e in sorted(self.active.items())
        ]
        lines.append(
            f"Queued: {len(self.queue)} | Active limit: {self.resources.plan.active_workers} | Shared encode threads: {self.resources.plan.encoder_threads}"
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
                selector = f"w{next(iter(self.active))}"
            elif not self.active and len(self.queue) == 1:
                selector = self.queue[0].job.id
            else:
                return "Use `.cancel W1`, `.cancel JOB_ID` or `.cancel all`."
        selected = [
            (w, e)
            for w, e in self.active.items()
            if selector in {"all", f"w{w}", e.job.id}
        ]
        queued = [e for e in self.queue if selector in {"all", e.job.id}]
        for entry in queued:
            self.bot.library.job(entry.job.id, status="cancelled")
            self.queue.remove(entry)
        tasks = [self.tasks[w] for w, _ in selected]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.event.set()
        return f"Cancelled {len(tasks)} active and {len(queued)} queued jobs. History and cached videos were kept."

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
