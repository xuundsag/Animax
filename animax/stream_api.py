"""Authenticated loopback API used only by the Go Live sidecar."""

from __future__ import annotations

import hmac
import logging

from aiohttp import web

from .streaming import StreamBusy, StreamManager, StreamRequestError

log = logging.getLogger("animax.stream_api")


def create_api(manager: StreamManager, secret: str):
    app = web.Application(client_max_size=128 * 1024)

    def authorized(request):
        supplied = request.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, f"Bearer {secret}")

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
    async def local_only(request, handler):
        if not authorized(request) and request.path != "/health":
            return json_response({"ok": False, "error": "Unauthorized."}, 401)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except (StreamRequestError, StreamBusy, ValueError, KeyError) as exc:
            return json_response({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            log.exception("Stream sidecar request failed: %s", request.path)
            return json_response(
                {"ok": False, "error": f"Internal ANIMAX error: {type(exc).__name__}."},
                500,
            )

    app.middlewares.append(local_only)

    async def health(_request):
        return json_response({"ok": True, "service": "animax-stream-coordinator"})

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

    async def segment_event(request):
        await manager.handle_segment_event(await body(request))
        return json_response({"ok": True})

    async def progress_event(request):
        await manager.handle_progress_event(await body(request))
        return json_response({"ok": True})

    async def series_event(request):
        await manager.handle_series_event(await body(request))
        return json_response({"ok": True})

    app.router.add_post("/stream/start", start_stream)
    app.router.add_post("/stream/skip", skip_stream)
    app.router.add_post("/stream/stop", stop_stream)
    app.router.add_post("/stream/status", stream_status)
    app.router.add_post("/internal/segment-event", segment_event)
    app.router.add_post("/internal/progress", progress_event)
    app.router.add_post("/internal/series-event", series_event)
    app.router.add_get("/health", health)
    return app
