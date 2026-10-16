"""Remove only downloader-owned files inside one episode's work directory."""

from __future__ import annotations

import re
from pathlib import Path

_FRAGMENT = re.compile(r"source\..*\.part-Frag\d+(?:\.part)?$")
_RANGE_ERROR = re.compile(
    r"\bHTTP(?:\s+Error)?[\s:]+416\b|\brange not satisfiable\b", re.I
)


def is_range_error(text: str) -> bool:
    return bool(_RANGE_ERROR.search(text))


def discard_fragment_files(work: Path) -> int:
    # Keep the assembled .part and .ytdl checkpoint. Re-fetch only uncommitted fragments.
    return _remove([path for path in work.iterdir() if _FRAGMENT.fullmatch(path.name)])


def reset_source_download(work: Path) -> int:
    # A whole-source retry must remove every selected format and its checkpoint together.
    paths = [
        *work.glob("source.*"),
        work / "source-fingerprint.txt",
        work / "progress.json",
        work / "progress.tmp",
    ]
    return _remove(paths)


def _remove(paths: list[Path]) -> int:
    count = 0
    for path in paths:
        if path.is_symlink() or path.is_file():
            path.unlink()
            count += 1
    return count
