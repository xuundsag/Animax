"""Check the current Python environment and install missing runtime dependencies."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))
MODULES = ("discord", "playwright", "dotenv", "yt_dlp", "aiohttp", "rich", "rapidfuzz")


def ensure_runtime():
    with (ROOT / ".bootstrap.lock").open("a") as lock:
        if os.name == "posix":
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _prepare_runtime()


def _prepare_runtime():
    if sys.version_info < (3, 10):
        raise SystemExit("ANX-SETUP: Python 3.10+ is required; 3.11+ is recommended.")
    for name in ("discord.py", "py-cord", "discord"):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        raise SystemExit(
            f"ANX-SETUP: {name} conflicts with discord.py-self. Remove the conflicting package and install requirements.txt in this Python environment."
        )
    missing = any(importlib.util.find_spec(module) is None for module in MODULES)
    try:
        missing = missing or metadata.version("discord.py-self") != "2.1.0"
    except metadata.PackageNotFoundError:
        missing = True
    if missing:
        print("ANIMAX | Installing missing Python packages...", flush=True)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                str(ROOT / "requirements.txt"),
            ]
        )
        if result.returncode:
            raise SystemExit(
                "ANX-SETUP: Run pip install -r requirements.txt using the same Python environment."
            )
    from playwright.sync_api import sync_playwright

    with sync_playwright() as browser:
        missing_browser = not Path(browser.chromium.executable_path).exists()
    if missing_browser:
        print("ANIMAX | Installing Chromium...", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"]
        )
        if result.returncode:
            raise SystemExit(
                "ANX-BROWSER: Chromium installation failed. Check internet access and its system libraries."
            )
