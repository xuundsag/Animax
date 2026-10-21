"""Authenticated localhost HTTP client for the single Node Discord/Go Live sidecar."""

from __future__ import annotations

import asyncio

import aiohttp


class BridgeError(RuntimeError):
    pass


class StreamBridge:
    def __init__(self, base_url: str, secret: str):
        self.base_url = base_url.rstrip("/")
        self.secret = secret

    async def _request(self, endpoint: str, payload=None, *, timeout=20):
        client_timeout = aiohttp.ClientTimeout(total=timeout, connect=8, sock_read=None)
        try:
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.post(
                    f"{self.base_url}{endpoint}",
                    json=payload or {},
                    headers={"Authorization": f"Bearer {self.secret}"},
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 400:
                        raise BridgeError(
                            str(
                                data.get("error")
                                or f"Stream bridge returned HTTP {response.status}."
                            )
                        )
                    if not data.get("ok", True):
                        raise BridgeError(
                            str(data.get("error") or "Stream bridge request failed.")
                        )
                    return data
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            TimeoutError,
            ValueError,
        ) as exc:
            raise BridgeError(
                f"Could not reach the Go Live sidecar: {type(exc).__name__}."
            ) from exc

    async def health(self):
        return await self._request("/api/health", timeout=5)

    async def send(self, channel_id: int, content: str):
        return await self._request(
            "/api/send", {"channelId": str(channel_id), "content": content[:1900]}
        )

    async def join(self, guild_id: int, voice_id: int):
        return await self._request(
            "/api/join",
            {"guildId": str(guild_id), "voiceChannelId": str(voice_id)},
            timeout=60,
        )

    async def series_start(
        self,
        series_id: str,
        guild_id: int,
        voice_id: int,
        *,
        bitrate: int,
        threads: int,
        preset: str,
    ):
        return await self._request(
            "/api/series/start",
            {
                "seriesId": series_id,
                "guildId": str(guild_id),
                "voiceChannelId": str(voice_id),
                "bitrate": int(bitrate),
                "threads": int(threads),
                "preset": preset,
            },
            timeout=60,
        )

    async def series_add(
        self,
        series_id: str,
        episode: int,
        kind: str,
        file_path: str,
        duration: float,
    ):
        return await self._request(
            "/api/series/add",
            {
                "seriesId": series_id,
                "episode": episode,
                "kind": kind,
                "filePath": file_path,
                "duration": duration,
            },
        )

    async def series_skip(self, series_id: str, episode: int):
        return await self._request(
            "/api/series/skip", {"seriesId": series_id, "episode": episode}
        )

    async def series_end(self, series_id: str):
        return await self._request("/api/series/end", {"seriesId": series_id})

    async def series_abort(self, series_id: str):
        return await self._request(
            "/api/series/abort", {"seriesId": series_id}, timeout=10
        )

    async def leave(self, guild_id: int):
        return await self._request("/api/leave", {"guildId": str(guild_id)}, timeout=10)
