"""Local capacity estimates and an optional short synthetic CPU-only encode."""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from .resources import GiB, apply_cpu_budget, disk_usage, host_capacity, plan_capacity


def report(root, cfg):
    host = host_capacity(root)
    return {
        "host": host,
        "plan": plan_capacity(cfg, host).data(),
        "cache_bytes": disk_usage(root / "downloads"),
        "disk_budget_bytes": cfg.max_temp_bytes,
        "limitations": "Heuristic, not a guaranteed maximum. Queue size is limited by RAM/disk. One transfer, one encoder and one uploader can overlap. Encoder threads are shared, not multiplied per worker. CPU affinity limits eligible logical CPUs on Linux, not total OS threads. Provider/Discord concurrency is not tested.",
    }


def report_text(root, cfg):
    data = report(root, cfg)
    host, plan = data["host"], data["plan"]
    return "\n".join(
        [
            f"Detected logical CPU slots: {host['cpu_slots']}",
            f"Shared encode threads: {plan['encoder_threads']} (requested {plan['requested_threads']})",
            f"Available RAM: {host['available_memory_bytes'] / GiB:.2f} GiB",
            f"Free disk: {host['free_disk_bytes'] / GiB:.2f} GiB",
            f"Cache: {data['cache_bytes'] / GiB:.2f} / {cfg.max_temp_bytes / GiB:.2f} GiB",
            f"Suggested active anime jobs: {plan['active_workers']} (heuristic)",
            "Shared slots: download 1 | encode 1 | upload 1",
            "Additional jobs wait in anime.json; no task per queued job.",
            "Not a VPS/provider/Discord reliability guarantee. Benchmark while idle.",
        ]
    )


def benchmark(root, cfg, plan):
    applied = apply_cpu_budget(plan["encoder_threads"])
    with tempfile.TemporaryDirectory(prefix=".capacity-", dir=root) as folder:
        output = Path(folder) / "synthetic.mp4"
        command = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=30",
            "-t",
            "8",
            "-filter_threads",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            cfg.encoder_preset,
            "-crf",
            str(cfg.video_crf),
            "-threads",
            str(plan["encoder_threads"]),
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(output),
        ]
        start = time.monotonic()
        subprocess.run(command, check=True, capture_output=True, timeout=180)
        elapsed = time.monotonic() - start
        return dict(
            synthetic=True,
            media_seconds=8,
            wall_seconds=round(elapsed, 3),
            realtime_multiple=round(8 / elapsed, 3),
            bytes=output.stat().st_size,
            resolution="1920x1080",
            fps=30,
            preset=cfg.encoder_preset,
            crf=cfg.video_crf,
            encoder_threads=plan["encoder_threads"],
            cpu_affinity_applied=applied,
            note="Synthetic encode only; not an episode, source download, Discord upload or sustained thermal/memory stress test.",
        )
