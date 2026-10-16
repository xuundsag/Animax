"""Host-aware admission and shared pipeline lanes; never a per-worker CPU budget."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

GiB = 1024**3
BASE_CPUS = (
    sorted(os.sched_getaffinity(0))
    if hasattr(os, "sched_getaffinity")
    else list(range(os.cpu_count() or 1))
)


def _number(path):
    try:
        value = Path(path).read_text().strip()
        return int(value) if value != "max" else None
    except (OSError, ValueError):
        return None


def host_capacity(root: Path):
    cpus = len(BASE_CPUS) or 1
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cpus = min(cpus, max(1, math.floor(int(quota) / int(period))))
    except (OSError, ValueError):
        quota = _number("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period = _number("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if quota and quota > 0 and period:
            cpus = min(cpus, max(1, quota // period))
    available = GiB
    try:
        values = {
            line.split(":")[0]: int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
        }
        available = values.get("MemAvailable", values.get("MemFree", GiB))
    except (OSError, ValueError):
        pass
    limit, used = (
        _number("/sys/fs/cgroup/memory.max"),
        _number("/sys/fs/cgroup/memory.current"),
    )
    if limit is None:
        limit, used = (
            _number("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            _number("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        )
    if limit and used is not None:
        available = min(available, max(0, limit - used))
    return {
        "cpu_slots": cpus,
        "available_memory_bytes": available,
        "free_disk_bytes": shutil.disk_usage(root).free,
    }


def disk_usage(root):
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            pass  # Download subprocesses rename/delete fragment files concurrently.
    return total


@dataclass(frozen=True)
class Plan:
    requested_threads: int
    encoder_threads: int
    active_workers: int
    download_lanes: int = 1
    encoder_lanes: int = 1
    upload_lanes: int = 1

    def data(self):
        return asdict(self)


def plan_capacity(cfg, host):
    memory_slots = max(
        1, (host["available_memory_bytes"] - 512 * 1024**2) // (384 * 1024**2)
    )
    disk_slots = max(
        1, min(cfg.max_temp_bytes, max(0, host["free_disk_bytes"] - GiB)) // (2 * GiB)
    )
    workers = max(1, min(cfg.max_workers or 3, memory_slots, disk_slots, 8))
    return Plan(
        cfg.encode_threads, min(cfg.encode_threads, host["cpu_slots"]), int(workers)
    )


def apply_cpu_budget(threads):
    """Linux affinity bounds eligible logical CPUs, not the number of OS threads."""
    if not hasattr(os, "sched_setaffinity"):
        return False
    cpus = set(BASE_CPUS[: max(1, threads)])
    try:
        os.sched_setaffinity(0, cpus)
        for task in Path("/proc/self/task").iterdir():
            try:
                os.sched_setaffinity(int(task.name), cpus)
            except ProcessLookupError:
                pass
        return True
    except OSError:
        return False


class Lease:
    def __init__(self, resources, name):
        self.resources, self.name, self.released = resources, name, False

    def release(self):
        if not self.released:
            self.released = True
            self.resources.occupied[self.name] -= 1
            if self.name == "transfer":
                self.resources.next_transfer = time.monotonic() + max(
                    self.resources.cfg.request_interval,
                    getattr(self.resources.cooldown, "interval", 0),
                )
            if self.name == "upload":
                self.resources.next_upload = time.monotonic() + self.resources.cfg.delay
            self.resources.gates[self.name].release()


class Resources:
    def __init__(self, root, cfg, on_wait=lambda job, text: None, cooldown=None):
        self.root, self.cfg, self.on_wait = Path(root), cfg, on_wait
        self.cooldown = cooldown
        self.disk_root = self.root / "downloads"
        self.host = host_capacity(self.root)
        self.plan = plan_capacity(cfg, self.host)
        self.gates = {
            name: asyncio.Semaphore(1) for name in ("transfer", "encode", "upload")
        }
        self.occupied = dict.fromkeys(self.gates, 0)
        self.peaks = dict.fromkeys(self.gates, 0)
        self.next_transfer = 0.0
        self.next_upload = 0.0

    def admission_reason(self):
        host = host_capacity(self.root)
        if host["available_memory_bytes"] < 256 * 1024**2:
            return "Waiting for RAM headroom"
        if host["free_disk_bytes"] < GiB:
            return "Waiting for free disk space"
        if disk_usage(self.disk_root) >= self.cfg.max_temp_bytes - 512 * 1024**2:
            return "Waiting for cache space; retained failed videos count toward the shared disk budget"
        return ""

    @contextlib.asynccontextmanager
    async def lease(self, name, job):
        labels = {
            "transfer": "Waiting download slot",
            "encode": "Waiting encoder",
            "upload": "Waiting uploader",
        }
        if job is not None and self.gates[name].locked():
            self.on_wait(job, labels[name])
        await self.gates[name].acquire()
        self.occupied[name] += 1
        self.peaks[name] = max(self.peaks[name], self.occupied[name])
        lease = Lease(self, name)
        try:
            if name == "transfer":
                await asyncio.sleep(max(0, self.next_transfer - time.monotonic()))
            if name == "upload":
                await asyncio.sleep(max(0, self.next_upload - time.monotonic()))
            yield lease
        finally:
            lease.release()
