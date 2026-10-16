"""Observe the normal embedded player; no hard-coded source keys or DRM bypass."""

from __future__ import annotations

import asyncio
import contextlib
import html
import os
import re
from pathlib import Path

os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path(__file__).resolve().parent.parent / ".browsers"),
)
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from aiohttp import web
from playwright.async_api import async_playwright

from .rate_limits import RateLimitError, parse_retry_after


class ResolveError(RuntimeError):
    pass


class LanguageUnavailable(ResolveError):
    """Positive provider evidence, not a timeout or transport failure."""

    pass


class CaptionUnavailable(ResolveError):
    """The selected caption track is positively missing or unusable."""

    pass


def unavailable_payload(payload):
    if not isinstance(payload, dict):
        return False
    message = str(payload.get("message") or payload.get("error") or "").casefold()
    if any(
        word in message
        for word in (
            "rate limit",
            "too many requests",
            "forbidden",
            "timeout",
            "timed out",
            "server error",
            "access denied",
        )
    ):
        return False
    if str(payload.get("status") or payload.get("code")) in {
        "401",
        "403",
        "429",
        "500",
        "502",
        "503",
        "504",
    }:
        return False
    if (
        "sources" in payload
        and payload["sources"] in (None, [])
        and not payload.get("enc")
    ):
        return True
    message = str(payload.get("message") or payload.get("error") or "").casefold()
    return bool(
        re.search(
            r"(?:episode|video|dub|source|file) (?:is |was )?(?:not found|unavailable|not available|deleted)|no (?:video|source|episode)s? (?:found|available)",
            message,
        )
    )


def unavailable_document(document):
    # MegaPlay sometimes serves its explicit 404/410 error page with HTTP 200.
    # Require its error-code element, not arbitrary script/player-error wording.
    block = re.search(
        r'<(?:div|span)\b[^>]*class=[\'"][^\'"]*\berror-code\b[^\'"]*[\'"][^>]*>(.*?)</div>',
        document,
        flags=re.I | re.S,
    )
    if not block:
        return False
    text = html.unescape(re.sub(r"<[^>]+>", " ", block.group(1)))
    return bool(re.search(r"Error\s+Code:\s*(?:404|410)\b", text, re.I))


def embed_url(mal_id: int, language: str, episode: int) -> str:
    if mal_id < 1 or episode < 1 or language not in {"dub", "sub"}:
        raise ValueError("Use positive MAL/episode numbers and dub or sub.")
    return f"https://megaplay.buzz/stream/mal/{mal_id}/{episode}/{language}"


def choose_caption(tracks: list[dict], language: str | None = None) -> dict | None:
    captions = [
        t
        for t in tracks
        if t.get("kind") in {"captions", "subtitles"}
        and urlsplit(str(t.get("file", ""))).scheme == "https"
    ]
    english = [
        t
        for t in captions
        if "english" in t.get("label", "").lower()
        or t.get("srclang", "").lower() in {"en", "eng"}
    ]
    if not english:
        return None

    def priority(track):
        label = track.get("label", "").lower()
        dub_match = language == "dub" and bool(
            re.search(r"\bdub(?:bed)?\b|\bcc\b|closed", label)
        )
        signs_only = bool(re.search(r"signs|songs", label))
        return (not signs_only, dub_match, bool(track.get("default")))

    return max(english, key=priority)


@dataclass
class Stream:
    url: str
    headers: dict[str, str]
    tracks: list[dict]
    context: object
    _prepared_track: dict | None = field(default=None, init=False, repr=False)
    _prepared_captions: bytes | None = field(default=None, init=False, repr=False)

    async def cookies(self):
        return await self.context.cookies()

    async def prepare_caption(self, language):
        track = choose_caption(self.tracks, language)
        if track is None:
            return False
        self._prepared_captions = await self.caption_bytes(track)
        self._prepared_track = track
        return True

    async def detach(self, language, with_captions=True):
        track = choose_caption(self.tracks, language) if with_captions else None
        if track and track == self._prepared_track:
            captions = self._prepared_captions
        else:
            captions = await self.caption_bytes(track) if track else None
        return DownloadSource(
            self.url,
            dict(self.headers),
            list(self.tracks),
            await self.cookies(),
            captions,
        )

    async def caption_bytes(self, track: dict) -> bytes:
        response = await self.context.request.get(
            track["file"], headers={"Referer": "https://megaplay.buzz/"}, timeout=30000
        )
        if response.status == 429:
            raise RateLimitError(parse_retry_after(response.headers.get("retry-after")))
        if not response.ok:
            error = (
                CaptionUnavailable
                if response.status in {400, 401, 403, 404, 410}
                else ResolveError
            )
            raise error(f"Subtitle server returned HTTP {response.status}.")
        data = await response.body()
        if len(data) > 10 * 1024 * 1024 or not data.lstrip().startswith(b"WEBVTT"):
            raise CaptionUnavailable(
                "The subtitle response is not a supported WebVTT file."
            )
        return data


@dataclass
class DownloadSource:
    url: str
    headers: dict[str, str]
    tracks: list[dict]
    cookie_data: list[dict]
    captions: bytes | None

    async def cookies(self):
        return self.cookie_data

    async def caption_bytes(self, track):
        if self.captions is None:
            raise ResolveError(
                "No caption data was captured before closing the browser."
            )
        return self.captions


class Resolver:
    def __init__(self, timeout: float = 60, headless: bool = True):
        self.timeout = timeout
        self.headless = headless
        self.runner = self.browser = self.playwright = None

    async def __aenter__(self):
        app = web.Application()

        async def player(request):
            try:
                url = embed_url(
                    int(request.query["mal"]),
                    request.query["lang"],
                    int(request.query["ep"]),
                )
            except (KeyError, ValueError):
                raise web.HTTPBadRequest()
            return web.Response(
                content_type="text/html",
                text=(
                    '<!doctype html><html><body style="margin:0;background:#000">'
                    f'<iframe src="{html.escape(url, quote=True)}" width="1280" height="720" '
                    'allow="autoplay; fullscreen" referrerpolicy="strict-origin-when-cross-origin" '
                    'style="border:0"></iframe></body></html>'
                ),
            )

        app.router.add_get("/", player)
        try:
            self.runner = web.AppRunner(app)
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await site.start()
            self.origin = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=self.headless)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        if self.runner:
            await self.runner.cleanup()

    @contextlib.asynccontextmanager
    async def resolve(self, mal_id: int, language: str, episode: int):
        """Resolve, close the playback page, and retain only its context/cookies."""
        embed_url(mal_id, language, episode)
        context = await self.browser.new_context(
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()

        async def avoid_playback_buffering(route):
            request = route.request
            path = urlsplit(request.url).path.lower()
            # Resolve playlists, but do not download the video a second time in Chromium.
            fragment = path.endswith((".ts", ".m4s", ".mp4", ".aac", ".vtt"))
            media = request.resource_type == "media" and not path.endswith(".m3u8")
            if fragment or media:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", avoid_playback_buffering)
        tracks: list[dict] = []
        found = asyncio.get_running_loop().create_future()
        tasks = set()
        source_status = []

        async def inspect(response):
            try:
                parsed = urlsplit(response.url)
                if response.status == 429 and not found.done():
                    player_frame = response.request.frame.url.startswith(
                        "https://megaplay.buzz/stream/"
                    )
                    media_path = parsed.path.lower().endswith(
                        (".m3u8", ".ts", ".m4s", ".mp4", ".aac")
                    )
                    if parsed.hostname == "megaplay.buzz" or (
                        player_frame and media_path
                    ):
                        found.set_exception(
                            RateLimitError(
                                parse_retry_after(response.headers.get("retry-after"))
                            )
                        )
                        return
                provider_source = parsed.hostname == "megaplay.buzz" and (
                    parsed.path.startswith("/stream/") or "getSources" in parsed.path
                )
                if (
                    provider_source
                    and response.status in (404, 410)
                    and not found.done()
                ):
                    found.set_exception(
                        LanguageUnavailable(
                            f"{language.upper()} episode {episode} unavailable: provider HTTP {response.status}."
                        )
                    )
                    return
                if (
                    provider_source
                    and parsed.path.startswith("/stream/")
                    and response.ok
                    and "html" in response.headers.get("content-type", "").lower()
                ):
                    if unavailable_document(await response.text()) and not found.done():
                        found.set_exception(
                            LanguageUnavailable(
                                f"{language.upper()} episode {episode}: provider displayed an explicit 404/410 availability error."
                            )
                        )
                        return
                if parsed.hostname == "megaplay.buzz" and "getSources" in parsed.path:
                    source_status.append(response.status)
                    if response.ok:
                        payload = await response.json()
                        if unavailable_payload(payload) and not found.done():
                            found.set_exception(
                                LanguageUnavailable(
                                    f"{language.upper()} episode {episode}: provider reported no playable source."
                                )
                            )
                            return
                        if isinstance(payload, dict):
                            tracks.extend(
                                t
                                for t in payload.get("tracks", [])
                                if isinstance(t, dict)
                            )
                # Only player-frame traffic, never top-level ads or popup media.
                frame_url = response.request.frame.url
                if not frame_url.startswith("https://megaplay.buzz/stream/"):
                    return
                if parsed.scheme != "https" or not response.ok:
                    return
                content_type = response.headers.get("content-type", "").lower()
                if ".m3u8" not in parsed.path.lower() and "mpegurl" not in content_type:
                    return
                body = await response.text()
                if body.lstrip().startswith("#EXTM3U") and not found.done():
                    request_headers = await response.request.all_headers()
                    headers = {
                        k: v
                        for k, v in request_headers.items()
                        if k.lower() in {"user-agent", "referer", "origin"}
                    }
                    found.set_result((response.url, headers))
            except Exception:
                # Other responses may be opaque or disappear during context shutdown.
                pass

        def observed(response):
            task = asyncio.create_task(inspect(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        async def dismiss_popup(popup):
            with contextlib.suppress(Exception):
                await popup.close()

        async def activate():
            while not found.done():
                for frame in page.frames:
                    if frame.url.startswith("https://megaplay.buzz/stream/"):
                        with contextlib.suppress(Exception):
                            await frame.locator(
                                ".jw-icon-display, .jw-display-icon-container"
                            ).first.click(timeout=700)
                await asyncio.sleep(2)

        page.on("response", observed)
        page.on("popup", dismiss_popup)
        clicker = None
        try:
            await page.goto(
                f"{self.origin}/?mal={mal_id}&lang={language}&ep={episode}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            clicker = asyncio.create_task(activate())
            try:
                url, headers = await asyncio.wait_for(found, self.timeout)
            except asyncio.TimeoutError:
                statuses = ", ".join(map(str, source_status)) or "no source response"
                raise ResolveError(
                    f"No playable HLS stream within {self.timeout:g}s ({statuses}). "
                    "Episode/language may be unavailable, unmapped, or blocked by the provider."
                ) from None
            # The source JSON precedes HLS; drain any still-running response handlers.
            if tasks:
                await asyncio.wait(list(tasks), timeout=2)
            # Stop the player from competing with the downloader for CDN requests.
            # Keep the isolated context/cookies/APIRequestContext for caption retrieval.
            if clicker:
                clicker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await clicker
                clicker = None
            await page.close()
            yield Stream(url, headers, list(tracks), context)
        finally:
            if clicker:
                clicker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await clicker
            for task in list(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)
            await context.close()


@contextlib.asynccontextmanager
async def resolve_with_fallback(resolver, mal, language, episode, allow_fallback=True):
    """Use sub only when dub or its English caption track is positively absent."""
    stack = contextlib.AsyncExitStack()
    await stack.__aenter__()
    reason = None
    actual = language
    try:
        try:
            stream = await stack.enter_async_context(
                resolver.resolve(mal, language, episode)
            )
        except LanguageUnavailable:
            if language != "dub" or not allow_fallback:
                raise
            reason = "dub unavailable"
            await stack.aclose()
            stack = contextlib.AsyncExitStack()
            await stack.__aenter__()
            actual = "sub"
            stream = await stack.enter_async_context(
                resolver.resolve(mal, "sub", episode)
            )
        else:
            if (
                language == "dub"
                and allow_fallback
                and getattr(stream, "tracks", None) is not None
            ):
                caption_available = choose_caption(stream.tracks, "dub") is not None
                prepare_caption = getattr(stream, "prepare_caption", None)
                if caption_available and prepare_caption is not None:
                    try:
                        caption_available = await prepare_caption("dub")
                    except CaptionUnavailable:
                        caption_available = False
                if not caption_available:
                    reason = "dub captions unavailable"
                    await stack.aclose()
                    stack = contextlib.AsyncExitStack()
                    await stack.__aenter__()
                    actual = "sub"
                    stream = await stack.enter_async_context(
                        resolver.resolve(mal, "sub", episode)
                    )
        yield stream, actual, reason
    finally:
        await stack.aclose()
