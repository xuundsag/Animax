"""Lazily start and supervise the Go Live sidecar for the additive .stream command."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from aiohttp import web

from .bridge import StreamBridge
from .stream_api import create_api
from .streaming import StreamManager

log = logging.getLogger("animax.stream_runtime")
NODE_MINIMUM = (22, 4, 0)
NODE_PACKAGES = {
    "@dank074/discord-video-stream": ("@dank074", "discord-video-stream", "7.0.0"),
    "@lng2004/discord.js-selfbot-v13": ("@lng2004", "discord.js-selfbot-v13", "3.7.2"),
}


def _version_tuple(text: str):
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", text)
    return tuple(map(int, match.groups())) if match else None


def _package_versions_match(root: Path):
    for scope, package, expected in NODE_PACKAGES.values():
        try:
            path = root / "node_modules" / scope / package / "package.json"
            version = json.loads(path.read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError):
            return False
        if version != expected:
            return False
    return True


def _validate_stream_health(health: dict, command_user_id: str | None = None):
    if not health.get("discordReady"):
        raise RuntimeError("The separate streaming Discord account is not ready.")
    stream_user_id = str(health.get("userId") or "")
    if not stream_user_id:
        raise RuntimeError("The streaming sidecar did not report its Discord user ID.")
    if command_user_id and stream_user_id == str(command_user_id):
        raise RuntimeError(
            "STREAM_DISCORD_TOKEN logged into the main command account; configure a different streaming user account."
        )


def _sidecar_environment(
    stream_token: str,
    root: Path,
    secret: str,
    api_port: int,
    inherited: dict[str, str] | None = None,
):
    environment = (os.environ if inherited is None else inherited).copy()
    environment.pop("DISCORD_TOKEN", None)
    environment.update(
        {
            "STREAM_DISCORD_TOKEN": stream_token,
            "ANIMAX_ROOT": str(root),
            "ANIMAX_API_KEY": secret,
            "ANIMAX_PYTHON_API": f"http://127.0.0.1:{api_port}",
        }
    )
    return environment


def _run_timestamped(command, *, cwd: Path, timeout: int):
    """Run setup commands while teeing each output line to the timestamped terminal."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines = queue.Queue()

    def read_output():
        try:
            for line in process.stdout:
                lines.put(line.rstrip("\r\n"))
        finally:
            lines.put(None)

    reader = threading.Thread(
        target=read_output, name="animax-stream-setup-log", daemon=True
    )
    reader.start()
    deadline = time.monotonic() + timeout
    finished_reading = False
    try:
        while not finished_reading:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                line = lines.get(timeout=min(1, remaining))
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    break
                continue
            if line is None:
                finished_reading = True
            else:
                print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)
        return process.wait(timeout=max(1, deadline - time.monotonic()))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join(timeout=2)


def _ensure_stream_dependencies(root: Path):
    print(
        f"[{time.strftime('%H:%M:%S')}] ANIMAX STREAM | Checking Node.js and FFmpeg...",
        flush=True,
    )
    node = shutil.which("node")
    if not node:
        raise RuntimeError(
            ".stream requires Node.js 22.4.0 or newer; install Node.js and retry."
        )
    result = subprocess.run(
        [node, "--version"], capture_output=True, text=True, timeout=10
    )
    version = _version_tuple(result.stdout or result.stderr)
    if result.returncode or not version or version < NODE_MINIMUM:
        found = (result.stdout or result.stderr).strip() or "unknown"
        raise RuntimeError(f".stream requires Node.js 22.4.0 or newer; found {found}.")

    filters = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    formats = subprocess.run(
        ["ffmpeg", "-hide_banner", "-formats"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    missing_filters = [name for name in ("drawtext",) if name not in filters.stdout]
    missing_encoders = [
        name for name in ("libx264", "aac") if name not in encoders.stdout
    ]
    if (
        filters.returncode
        or encoders.returncode
        or formats.returncode
        or missing_filters
        or missing_encoders
        or "mpegts" not in formats.stdout
    ):
        raise RuntimeError(
            ".stream requires CPU FFmpeg with libx264, AAC, drawtext, and MPEG-TS support."
        )
    print(
        f"[{time.strftime('%H:%M:%S')}] ANIMAX STREAM | FFmpeg preflight passed.",
        flush=True,
    )

    if _package_versions_match(root):
        return
    lock = root / "package-lock.json"
    npm = shutil.which("npm")
    if not lock.exists() or not npm:
        raise RuntimeError(
            ".stream dependencies are missing; restore package-lock.json and npm, then retry."
        )
    print(
        f"[{time.strftime('%H:%M:%S')}] ANIMAX | Installing Go Live dependencies for .stream...",
        flush=True,
    )
    returncode = _run_timestamped(
        [npm, "ci", "--omit=dev", "--no-audit", "--no-fund"],
        cwd=root,
        timeout=900,
    )
    if returncode or not _package_versions_match(root):
        raise RuntimeError(
            "Could not install the pinned .stream dependencies; see npm output above."
        )


async def _read_node_stdout(process, ready: asyncio.Event, port_future, node_log=None):
    """Print every Node stdout/stderr line live and retain a private sidecar log."""
    assert process.stdout is not None
    while line := await process.stdout.readline():
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] ANIMAX STREAM NODE | {text[:1200]}", flush=True)
        if node_log is not None:
            try:
                node_log.write(f"[{timestamp}] {text}\n")
                node_log.flush()
            except (OSError, ValueError):
                pass
        try:
            record = json.loads(text)
        except ValueError:
            log.info("SIDECAR: %s", text[:500])
            continue
        if record.get("event") == "discord-ready":
            ready.set()
            log.info(
                "Go Live sidecar logged in (user id %s).",
                record.get("userId", "unknown"),
            )
        elif record.get("event") == "http-ready":
            if not port_future.done():
                try:
                    port_future.set_result(int(record["port"]))
                except (KeyError, TypeError, ValueError):
                    port_future.set_exception(
                        RuntimeError("Sidecar announced an invalid API port.")
                    )
            log.info("Go Live sidecar local API is ready.")
        else:
            log.info("SIDECAR: %s", text[:500])


class StreamRuntime:
    """Own the local Python API and single Node Go Live session, started on first use."""

    def __init__(
        self,
        token: str,
        settings,
        root: Path,
        *,
        command_user_id: str | None = None,
        library=None,
        attachment_lookup=None,
        ui=None,
    ):
        self.token = token
        self.command_user_id = str(command_user_id) if command_user_id else None
        self.settings = settings
        self.root = Path(root)
        self.ui = ui
        self.secret = os.urandom(32).hex()
        self.bridge = StreamBridge("http://127.0.0.1:1", self.secret)
        self.manager = StreamManager(
            settings,
            self.bridge,
            self.root,
            library=library,
            attachment_lookup=attachment_lookup,
            ui=ui,
            command_account_id=self.command_user_id,
        )
        self.runner = None
        self.process = None
        self.process_wait = None
        self.reader_task = None
        self.node_log = None
        self.started = False
        self._start_lock = asyncio.Lock()

    def _event(self, message: str, style="cyan"):
        log.info("STREAM_RUNTIME %s", message)
        if self.ui is not None and hasattr(self.ui, "event"):
            self.ui.event(f"STREAM | {message}", style)
        else:
            print(
                f"[{time.strftime('%H:%M:%S')}] ANIMAX STREAM | {message}", flush=True
            )

    async def start(self):
        async with self._start_lock:
            if self.started:
                return self.manager
            self._event("Checking stream runtime dependencies.")
            await asyncio.to_thread(_ensure_stream_dependencies, self.root)
            self.runner = web.AppRunner(
                create_api(self.manager, self.secret), access_log=None
            )
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await site.start()
            api_port = self.runner.addresses[0][1]
            logs = self.root / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            log_path = logs / "stream-bridge.log"
            self.node_log = log_path.open("a", encoding="utf-8")
            log_path.chmod(0o600)
            environment = _sidecar_environment(
                self.token, self.root, self.secret, api_port
            )
            node = shutil.which("node")
            try:
                self.process = await asyncio.create_subprocess_exec(
                    node,
                    str(self.root / "animax" / "stream_bridge.mjs"),
                    cwd=str(self.root),
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                ready = asyncio.Event()
                port_future = asyncio.get_running_loop().create_future()
                self.process_wait = asyncio.create_task(self.process.wait())
                self.reader_task = asyncio.create_task(
                    _read_node_stdout(self.process, ready, port_future, self.node_log)
                )
                self._event("Waiting for the streaming Discord account to log in.")
                ready_wait = asyncio.create_task(ready.wait())
                deadline = time.monotonic() + 120
                while not ready_wait.done() and not self.process_wait.done():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    done, _ = await asyncio.wait(
                        {self.process_wait, ready_wait},
                        timeout=min(15, remaining),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        elapsed = int(120 - max(0, deadline - time.monotonic()))
                        self._event(
                            f"Still waiting for streaming account login ({elapsed}s).",
                            "yellow",
                        )
                if self.process_wait.done():
                    ready_wait.cancel()
                    await asyncio.gather(ready_wait, return_exceptions=True)
                    raise RuntimeError(
                        f"Go Live sidecar exited during startup (code {self.process.returncode}); see logs/stream-bridge.log."
                    )
                if not ready_wait.done():
                    ready_wait.cancel()
                    await asyncio.gather(ready_wait, return_exceptions=True)
                    raise RuntimeError(
                        "Timed out waiting for Go Live sidecar login; see logs/stream-bridge.log."
                    )
                done, _ = await asyncio.wait(
                    {self.process_wait, port_future},
                    timeout=5,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if self.process_wait in done:
                    raise RuntimeError(
                        f"Go Live sidecar exited during startup (code {self.process.returncode}); see logs/stream-bridge.log."
                    )
                if port_future not in done:
                    raise RuntimeError(
                        "Go Live sidecar did not publish its local API port."
                    )
                self.bridge.base_url = f"http://127.0.0.1:{port_future.result()}"
                health = await self.bridge.health()
                _validate_stream_health(health, self.command_user_id)
                self.started = True
                log.info(".stream Go Live sidecar is ready.")
                self._event(
                    f"Go Live sidecar ready as distinct user {health.get('userId')}; main DISCORD_TOKEN was not passed to Node.",
                    "green",
                )
                return self.manager
            except BaseException:
                await self.close()
                raise

    async def close(self):
        session = self.manager.session
        if session and session.task and not session.task.done():
            session.stop_event.set()
            for task in (session.prepare_task, session.prefetch_task):
                if task and not task.done():
                    task.cancel()
            if session.series_started:
                with contextlib.suppress(Exception):
                    await self.bridge.series_abort(session.id)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.task, timeout=15)

        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                waiter = self.process_wait or asyncio.create_task(self.process.wait())
                await asyncio.wait_for(asyncio.shield(waiter), timeout=8)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader_task:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
            self.reader_task = None
        if self.node_log:
            with contextlib.suppress(Exception):
                self.node_log.close()
            self.node_log = None
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        self.started = False
