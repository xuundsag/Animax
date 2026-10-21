"""Disk-usage accounting for the single stream cache and its one prefetch slot."""

from __future__ import annotations

from pathlib import Path


def disk_usage(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            # Downloader fragment files may be renamed or removed concurrently.
            pass
    return total
