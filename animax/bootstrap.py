"""Standard-library bootstrap. Installs only into this project's private .venv."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))
MODULES = ("discord", "playwright", "dotenv", "yt_dlp", "aiohttp", "rich", "rapidfuzz")


def serialized_setup(function):
    def wrapped():
        with (ROOT / ".bootstrap.lock").open("a") as lock:
            if os.name == "posix":
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return function()

    return wrapped


@serialized_setup
def ensure_runtime():
    if sys.version_info < (3, 10):
        raise SystemExit(
            "ANX-SETUP: Python 3.10+ is required; Python 3.11+ is recommended."
        )
    env = ROOT / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if Path(sys.prefix).resolve() != env.resolve():
        if os.getenv("ANIMAX_BOOTSTRAP_REEXEC") == "1":
            raise SystemExit(
                "ANX-SETUP: virtual environment restart failed. Delete only .venv and rerun bot.py."
            )
        if not python.exists():
            print("ANIMAX  |  Preparing private Python environment...", flush=True)
            result = subprocess.run([sys.executable, "-m", "venv", str(env)])
            if result.returncode:
                raise SystemExit(
                    "ANX-SETUP: install python3-venv (Ubuntu/Debian), then run bot.py again."
                )
        environment = dict(os.environ, ANIMAX_BOOTSTRAP_REEXEC="1")
        os.execve(
            str(python), [str(python), str(ROOT / "bot.py"), *sys.argv[1:]], environment
        )
    # Verify distributions too: the plain "discord" namespace can belong to the wrong library.
    from importlib import metadata

    needed = any(importlib.util.find_spec(module) is None for module in MODULES)
    try:
        needed = needed or metadata.version("discord.py-self") != "2.1.0"
    except metadata.PackageNotFoundError:
        needed = True
    conflicting = []
    for name in ("discord.py", "py-cord", "discord"):
        try:
            metadata.version(name)
            conflicting.append(name)
        except metadata.PackageNotFoundError:
            pass
    if needed or conflicting or "--repair" in sys.argv:
        print(
            "ANIMAX  |  Installing/repairing missing Python packages. Please wait...",
            flush=True,
        )
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        with (logs / "bootstrap.log").open("a") as log:
            if conflicting:
                subprocess.run(
                    [sys.executable, "-m", "pip", "uninstall", "-y", *conflicting],
                    stdout=log,
                    stderr=log,
                )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--upgrade",
                    "-r",
                    str(ROOT / "requirements.txt"),
                ],
                stdout=log,
                stderr=log,
            )
            if result.returncode == 0 and conflicting:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--force-reinstall",
                        "--no-deps",
                        "discord.py-self==2.1.0",
                    ],
                    stdout=log,
                    stderr=log,
                )
        if result.returncode:
            raise SystemExit(
                "ANX-SETUP: package installation failed. Details: logs/bootstrap.log. Check internet/PyPI access."
            )
        print("ANIMAX  |  Python packages are ready.", flush=True)
    # Browser binaries are separate from pip packages. OS libraries require install.sh.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        missing = not Path(p.chromium.executable_path).exists()
    if missing or "--repair" in sys.argv:
        print("ANIMAX  |  Installing Chromium browser...", flush=True)
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        with (logs / "bootstrap.log").open("a") as log:
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                stdout=log,
                stderr=log,
            )
        if result.returncode:
            raise SystemExit(
                "ANX-BROWSER: Chromium installation failed. Details: logs/bootstrap.log."
            )
