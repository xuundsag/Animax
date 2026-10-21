"""Python coordinator and sole launcher target; Node owns one DAVE-capable session."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import shutil
import signal
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from .bootstrap import ROOT
from .bridge import StreamBridge
from .diagnostics import SetupError, dependency_report
from .settings import Settings
from .streaming import StreamBusy, StreamManager, StreamRequestError
from .terminal import setup_logging

log = logging.getLogger("animax")


def create_api(manager: StreamManager, secret: str):
    app = web.Application(client_max_size=128 * 1024)

    def authorized(request):
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {secret}"
        return hmac.compare_digest(supplied, expected)

    async def body(request):
        if not authorized(request):
            raise web.HTTPUnauthorized(text="Unauthorized")
        try:
            value = await request.json()
        except (ValueError, web.HTTPBadRequest):
            raise web.HTTPBadRequest(text="Expected a JSON request body.") from None
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object.")
        return value

    def json_response(data, status=200):
        return web.json_response(
            data, status=status, headers={"Cache-Control": "no-store"}
        )

    @web.middleware
    async def no_external_access(request, handler):
        # The HTTP bridge is bound to loopback and the bearer secret is still required.
        if not authorized(request) and request.path != "/health":
            return json_response({"ok": False, "error": "Unauthorized."}, 401)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except (StreamRequestError, StreamBusy, ValueError) as exc:
            return json_response({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            log.exception("Local bridge request failed: %s", request.path)
            return json_response(
                {"ok": False, "error": f"Internal ANIMAX error: {type(exc).__name__}."},
                500,
            )

    app.middlewares.append(no_external_access)

    async def health(_request):
        return json_response({"ok": True, "service": "animax-python-coordinator"})

    async def start_stream(request):
        data = await body(request)
        return json_response(
            await manager.start(
                guild_id=int(data["guildId"]),
                voice_channel_id=int(data["voiceChannelId"]),
                text_channel_id=int(data["channelId"]),
                author_id=int(data["authorId"]),
                raw=str(data.get("raw", "")),
            )
        )

    async def skip_stream(request):
        data = await body(request)
        return json_response(
            await manager.skip(
                guild_id=int(data["guildId"]), author_id=int(data["authorId"])
            )
        )

    async def stop_stream(request):
        data = await body(request)
        return json_response(
            await manager.stop(
                guild_id=int(data["guildId"]), author_id=int(data["authorId"])
            )
        )

    async def stream_status(request):
        data = await body(request)
        return json_response(manager.status(int(data["guildId"])))

    async def settings(request):
        data = await body(request)
        if data.get("action") == "set":
            if not data.get("name") or not data.get("value"):
                raise StreamRequestError(
                    "Use `.settings <name> <value>` to change a setting."
                )
            return json_response(
                await manager.settings_result(str(data["name"]), str(data["value"]))
            )
        if data.get("name") and not data.get("value"):
            return json_response(await manager.settings_result(str(data["name"]), ""))
        return json_response(await manager.settings_result())

    async def segment_event(request):
        data = await body(request)
        await manager.handle_segment_event(data)
        return json_response({"ok": True})

    async def series_event(request):
        data = await body(request)
        await manager.handle_series_event(data)
        return json_response({"ok": True})

    app.router.add_post("/stream/start", start_stream)
    app.router.add_post("/stream/skip", skip_stream)
    app.router.add_post("/stream/stop", stop_stream)
    app.router.add_post("/stream/status", stream_status)
    app.router.add_post("/settings/get", settings)
    app.router.add_post("/internal/segment-event", segment_event)
    app.router.add_post("/internal/series-event", series_event)
    app.router.add_get("/health", health)
    return app


async def _read_node_stdout(process, ready: asyncio.Event, port_future):
    assert process.stdout is not None
    while line := await process.stdout.readline():
        text = line.decode("utf-8", "replace").strip()
        try:
            record = json.loads(text)
        except ValueError:
            log.info("SIDECAR: %s", text[:500])
            continue
        if record.get("event") == "discord-ready":
            ready.set()
            log.info(
                "Discord sidecar logged in (user id %s).",
                record.get("userId", "unknown"),
            )
        elif record.get("event") == "http-ready":
            if not port_future.done():
                port_future.set_result(int(record["port"]))
            log.info("Local Go Live sidecar HTTP bridge is listening.")
        else:
            log.info("SIDECAR: %s", text[:500])


def _stop_node(process, logfile):
    if logfile:
        with contextlib.suppress(Exception):
            logfile.close()
    if process and process.returncode is None:
        process.terminate()


async def run_service(token: str, settings: Settings, root: Path = ROOT):
    secret = secrets.token_urlsafe(32)
    bridge = StreamBridge("http://127.0.0.1:1", secret)
    manager = StreamManager(settings, bridge, root)
    runner = web.AppRunner(create_api(manager, secret), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    api_port = runner.addresses[0][1]
    bridge.base_url = (
        "http://127.0.0.1:0"  # Set after the sidecar announces its ephemeral port.
    )

    node = shutil.which("node")
    if not node:
        await runner.cleanup()
        raise RuntimeError("Node.js 22.4+ is required for Go Live.")
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    node_log = (logs / "stream-bridge.log").open("a", encoding="utf-8")
    node_log_path = logs / "stream-bridge.log"
    node_log_path.chmod(0o600)
    environment = os.environ.copy()
    environment.update(
        {
            "DISCORD_TOKEN": token,
            "ANIMAX_ROOT": str(root),
            "ANIMAX_API_KEY": secret,
            "ANIMAX_PYTHON_API": f"http://127.0.0.1:{api_port}",
            "ANIMAX_PREFIX": ".",
        }
    )
    process = None
    process_wait = None
    reader_task = None
    try:
        process = await asyncio.create_subprocess_exec(
            node,
            str(root / "animax" / "stream_bridge.mjs"),
            cwd=str(root),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=node_log,
        )
        sidecar_ready = asyncio.Event()
        port_future = asyncio.get_running_loop().create_future()
        process_wait = asyncio.create_task(process.wait())
        reader_task = asyncio.create_task(
            _read_node_stdout(process, sidecar_ready, port_future)
        )
        ready_wait = asyncio.create_task(sidecar_ready.wait())
        done, _ = await asyncio.wait(
            {process_wait, ready_wait},
            timeout=120,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if process_wait in done:
            ready_wait.cancel()
            await asyncio.gather(ready_wait, return_exceptions=True)
            raise RuntimeError(
                f"Go Live sidecar exited during startup (code {process.returncode}); see logs/stream-bridge.log."
            )
        if ready_wait not in done:
            ready_wait.cancel()
            await asyncio.gather(ready_wait, return_exceptions=True)
            raise RuntimeError(
                "Timed out waiting for Discord self-bot login; see logs/stream-bridge.log."
            )
        port_wait = asyncio.ensure_future(port_future)
        done, _ = await asyncio.wait(
            {process_wait, port_wait},
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if process_wait in done:
            raise RuntimeError(
                f"Go Live sidecar exited during startup (code {process.returncode}); see logs/stream-bridge.log."
            )
        if port_wait not in done:
            raise RuntimeError("Go Live sidecar did not publish its local bridge port.")
        node_port = port_wait.result()
        bridge.base_url = f"http://127.0.0.1:{node_port}"
        await bridge.health()
        log.info(
            "ANIMAX is ready. Commands: .stream, .skip, .stop, .status, .settings, .help"
        )

        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signum, shutdown.set)
        shutdown_wait = asyncio.create_task(shutdown.wait())
        done, _ = await asyncio.wait(
            {process_wait, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        if shutdown_wait not in done:
            shutdown_wait.cancel()
            await asyncio.gather(shutdown_wait, return_exceptions=True)
        if process_wait in done and not shutdown.is_set():
            raise RuntimeError(
                f"Go Live sidecar exited unexpectedly (code {process.returncode}); see logs/stream-bridge.log."
            )
    finally:
        session = manager.session
        if session and session.task and not session.task.done():
            session.stop_event.set()
            for task in (session.prepare_task, session.prefetch_task):
                if task and not task.done():
                    task.cancel()
            if session.series_started:
                with contextlib.suppress(Exception):
                    await bridge.series_abort(session.id)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.task, timeout=15)
        _stop_node(process, node_log)
        if process and process.returncode is None:
            try:
                waiter = process_wait or asyncio.create_task(process.wait())
                await asyncio.wait_for(asyncio.shield(waiter), timeout=8)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if reader_task:
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
        await runner.cleanup()


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+")
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                self.file.seek(0)
                self.file.write(" ")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            raise RuntimeError("Another ANIMAX process is already running.") from None
        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()
        return self

    def __exit__(self, *_args):
        if self.file:
            self.file.close()


def main():
    load_dotenv(ROOT / ".env")
    setup_logging(ROOT)
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "YOUR_USER_TOKEN_HERE":
        raise SystemExit(
            "ANX-CONFIG: Set DISCORD_TOKEN locally in .env. Never share the token."
        )
    try:
        report = dependency_report()
        log.info("RUNTIME_CHECK %s", report)
        settings = Settings.from_env(ROOT / "settings.json")
        with InstanceLock(ROOT / ".animax.lock"):
            asyncio.run(run_service(token, settings, ROOT))
    except (SetupError, ValueError, RuntimeError) as exc:
        log.exception("Startup stopped")
        print(f"ANIMAX startup failed: {exc}")
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass
