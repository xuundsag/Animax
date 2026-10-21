"""Check the current Python environment and install missing runtime dependencies."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))
MODULES = ("discord", "playwright", "dotenv", "yt_dlp", "aiohttp", "rich", "rapidfuzz")


def _fatal(message, status=1):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)
    raise SystemExit(status)


def ensure_runtime():
    with (ROOT / ".bootstrap.lock").open("a") as lock:
        if os.name == "posix":
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _prepare_runtime()


def _run_timestamped(command):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            print(f"[{time.strftime('%H:%M:%S')}] {line.rstrip()}", flush=True)
        return process.wait()
    except BaseException:
        if process.poll() is None:
            process.terminate()
            process.wait()
        raise


def _prepare_runtime():
    if sys.version_info < (3, 10):
        _fatal("ANX-SETUP: Python 3.10+ is required; 3.11+ is recommended.")
    for name in ("discord.py", "py-cord", "discord"):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        _fatal(
            f"ANX-SETUP: {name} conflicts with discord.py-self. Remove the conflicting package and install requirements.txt in this Python environment."
        )
    missing = any(importlib.util.find_spec(module) is None for module in MODULES)
    try:
        missing = missing or metadata.version("discord.py-self") != "2.1.0"
    except metadata.PackageNotFoundError:
        missing = True
    if missing:
        print(
            f"[{time.strftime('%H:%M:%S')}] ANIMAX | Installing missing Python packages...",
            flush=True,
        )
        returncode = _run_timestamped(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                str(ROOT / "requirements.txt"),
            ]
        )
        if returncode:
            _fatal(
                "ANX-SETUP: Run pip install -r requirements.txt using the same Python environment."
            )
    from playwright.sync_api import sync_playwright

    with sync_playwright() as browser:
        missing_browser = not Path(browser.chromium.executable_path).exists()
    if missing_browser:
        print(
            f"[{time.strftime('%H:%M:%S')}] ANIMAX | Installing Chromium...",
            flush=True,
        )
        returncode = _run_timestamped(
            [sys.executable, "-m", "playwright", "install", "chromium"]
        )
        if returncode:
            _fatal(
                "ANX-BROWSER: Chromium installation failed. Check internet access and its system libraries."
            )
