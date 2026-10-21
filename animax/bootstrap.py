"""Validate Python/Node runtimes and install local dependencies before launch."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))
PYTHON_MODULES = ("playwright", "dotenv", "yt_dlp", "aiohttp", "rich", "rapidfuzz")
NODE_MINIMUM = (22, 4, 0)


def ensure_runtime():
    with (ROOT / ".bootstrap.lock").open("a") as lock:
        if os.name == "posix":
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _prepare_runtime()


def _version_tuple(text: str):
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", text)
    return tuple(map(int, match.groups())) if match else None


def _prepare_runtime():
    if sys.version_info < (3, 10):
        raise SystemExit("ANX-SETUP: Python 3.10+ is required; 3.11+ is recommended.")
    missing = any(importlib.util.find_spec(module) is None for module in PYTHON_MODULES)
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
            ],
            cwd=ROOT,
        )
        if result.returncode:
            raise SystemExit(
                "ANX-SETUP: Run pip install -r requirements.txt using this Python environment."
            )

    node = shutil.which("node")
    if not node:
        raise SystemExit(
            "ANX-NODE: Node.js 22.4+ is required for the DAVE-capable Go Live sidecar."
        )
    result = subprocess.run([node, "--version"], capture_output=True, text=True)
    node_version = _version_tuple(result.stdout or result.stderr)
    if not node_version or node_version < NODE_MINIMUM:
        found = (result.stdout or result.stderr).strip() or "unknown"
        raise SystemExit(f"ANX-NODE: Node.js 22.4+ is required; found {found}.")
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit(
            "ANX-NODE: npm is required to install the Go Live sidecar dependencies."
        )

    # Reinstall stale or partial node_modules using the committed lockfile.
    node_packages = {
        "@dank074/discord-video-stream": (
            "@dank074",
            "discord-video-stream",
            "7.0.0",
        ),
        "@lng2004/discord.js-selfbot-v13": (
            "@lng2004",
            "discord.js-selfbot-v13",
            "3.7.2",
        ),
    }
    modules_ready = True
    for scope, package, expected in node_packages.values():
        package_file = ROOT / "node_modules" / scope / package / "package.json"
        try:
            installed = json.loads(package_file.read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError):
            modules_ready = False
            break
        if installed != expected:
            modules_ready = False
            break
    if not modules_ready:
        if not (ROOT / "package-lock.json").exists():
            raise SystemExit(
                "ANX-NODE: package-lock.json is missing; restore the release files before launch."
            )
        print("ANIMAX | Installing DAVE-capable Go Live dependencies...", flush=True)
        result = subprocess.run(
            [npm, "ci", "--omit=dev", "--no-audit", "--no-fund"],
            cwd=ROOT,
        )
        if result.returncode:
            raise SystemExit(
                "ANX-NODE: npm dependency installation failed. See the output above."
            )

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit(
            "ANX-FFMPEG: FFmpeg and FFprobe must be installed and available in PATH."
        )

    from playwright.sync_api import sync_playwright

    with sync_playwright() as browser:
        missing_browser = not Path(browser.chromium.executable_path).exists()
    if missing_browser:
        print("ANIMAX | Installing Chromium...", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"], cwd=ROOT
        )
        if result.returncode:
            raise SystemExit(
                "ANX-BROWSER: Chromium installation failed. Check internet access and system libraries."
            )

    # Verify both pinned packages after installation and before connecting.
    for name, (scope, package, expected) in node_packages.items():
        package_file = ROOT / "node_modules" / scope / package / "package.json"
        try:
            installed = json.loads(package_file.read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError):
            raise SystemExit(
                "ANX-NODE: The Go Live sidecar dependencies are not installed correctly."
            ) from None
        if installed != expected:
            raise SystemExit(
                f"ANX-NODE: ANIMAX requires {name} {expected}; found {installed}."
            )
