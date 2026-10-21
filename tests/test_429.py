import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from animax.media import download
from animax.rate_limits import RateLimitError
from animax.resolver import Stream


@pytest.mark.asyncio
async def test_real_downloader_stops_after_first_429_fragment(tmp_path):
    fragment_requests = []

    async def playlist(request):
        return web.Response(
            text="#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:5,\nsegment.ts\n#EXT-X-ENDLIST\n",
            content_type="application/vnd.apple.mpegurl",
        )

    async def fragment(request):
        fragment_requests.append(request.path)
        return web.Response(
            status=429, headers={"Retry-After": "123"}, text="Too Many Requests"
        )

    app = web.Application()
    app.router.add_get("/index.m3u8", playlist)
    app.router.add_get("/segment.ts", fragment)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    stream = Stream(
        f"http://127.0.0.1:{port}/index.m3u8",
        {},
        [],
        SimpleNamespace(cookies=AsyncMock(return_value=[])),
    )
    try:
        with pytest.raises(RateLimitError) as caught:
            await download(
                stream, tmp_path / "work", "dub", request_interval=0, timeout=30
            )
        assert caught.value.retry_after == 123
        assert fragment_requests == ["/segment.ts"]  # NOT four immediate attempts
        assert (
            json.loads((tmp_path / "work/rate-limit.json").read_text())["status"] == 429
        )
    finally:
        await runner.cleanup()
