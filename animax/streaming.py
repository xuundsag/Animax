"""Single-session, continuous episode streaming with one next-episode prefetch."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .bridge import BridgeError, StreamBridge
from .encoding import FONT_FILES
from .metadata import TitleError, provisional_title, resolve_anime
from .resolver import (
    CaptionUnavailable,
    Resolver,
    choose_caption,
    resolve_with_fallback,
)
from .settings import Settings
from .stream_media import MediaError, cached_video, download, probe
from .titlecard import create_title_card

log = logging.getLogger("animax.streaming")
ROOT = Path(__file__).resolve().parent.parent
SKIPPED = object()
STREAM_ENCODER_PRESET = "ultrafast"


class StreamRequestError(ValueError):
    pass


class StreamBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class StreamRequest:
    query: str
    start: int
    end: int
    skip_intro_outro: bool
    language: str = "sub"


def parse_stream_request(raw: str) -> StreamRequest:
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise StreamRequestError(f"Could not parse the command: {exc}") from None
    if len(tokens) < 3:
        raise StreamRequestError(
            "Usage: `.stream <MAL ID or anime name> <start episode> <end episode> [Yes|No] [sub|dub]`."
        )
    language = "sub"
    skip = False
    seen_language = seen_skip = False
    while tokens and tokens[-1].casefold() in {"yes", "no", "sub", "dub"}:
        option = tokens.pop().casefold()
        if option in {"sub", "dub"}:
            if seen_language:
                raise StreamRequestError("Choose only one audio language: sub or dub.")
            language = option
            seen_language = True
        else:
            if seen_skip:
                raise StreamRequestError("Specify skip times only once: Yes or No.")
            skip = option == "yes"
            seen_skip = True
    if len(tokens) < 3:
        raise StreamRequestError(
            "Enter an anime title (or MAL ID) and an episode range."
        )
    try:
        start, end = int(tokens[-2]), int(tokens[-1])
    except ValueError:
        raise StreamRequestError(
            "The start and end episodes must be whole numbers."
        ) from None
    query = " ".join(tokens[:-2]).strip()
    if not query:
        raise StreamRequestError("Enter a MAL ID or anime name.")
    if start < 1 or end < start or end - start + 1 > 100:
        raise StreamRequestError(
            "Use a positive inclusive range of no more than 100 episodes."
        )
    return StreamRequest(query, start, end, skip, language)


@dataclass
class PreparedEpisode:
    episode: int
    work: Path
    path: Path
    duration: float
    note: str | None = None


@dataclass
class StreamSession:
    id: str
    guild_id: int
    voice_channel_id: int
    text_channel_id: int
    author_id: int
    request: StreamRequest
    phase: str = "Finding anime"
    mal_id: int | None = None
    title: str = ""
    current_episode: int | None = None
    preparing_episode: int | None = None
    prefetch_episode: int | None = None
    prefetch_started_at: float | None = None
    task: asyncio.Task | None = None
    prepare_task: asyncio.Task | None = None
    prefetch_task: asyncio.Task | None = None
    series_started: bool = False
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    series_finished: asyncio.Event = field(default_factory=asyncio.Event)
    episode_events: dict[int, asyncio.Event] = field(default_factory=dict)
    episode_results: dict[int, dict] = field(default_factory=dict)
    skip_preparing: set[int] = field(default_factory=set)
    node_error: str = ""
    fallback_notice_sent: bool = False
    caption_notice_sent: bool = False
    skip_notice_sent: bool = False
    card_directory: Path | None = None
    playback_episode: int | None = None
    playback_state: str = "Preparing"
    playback_elapsed: float = 0.0
    playback_duration: float = 0.0
    playback_updated_at: float | None = None
    status_started_at: float | None = None
    progress_message_id: str | None = None
    progress_message_text: str = ""
    progress_task: asyncio.Task | None = None


class StreamManager:
    def __init__(self, settings: Settings, bridge: StreamBridge, root: Path = ROOT):
        self.settings = settings
        self.bridge = bridge
        self.root = root
        self.cache_root = root / "downloads" / "stream-cache"
        self.disk_root = root / "downloads"
        self.session: StreamSession | None = None
        self.lock = asyncio.Lock()

    async def _send(self, session: StreamSession, text: str):
        try:
            return await self.bridge.send(session.text_channel_id, text)
        except BridgeError:
            log.warning(
                "Could not send stream status to channel %s", session.text_channel_id
            )
            return None

    async def _update_progress_message(self, session: StreamSession):
        content = self._progress_text(session)
        if content == session.progress_message_text:
            return
        try:
            if session.progress_message_id:
                await self.bridge.edit(
                    session.text_channel_id, session.progress_message_id, content
                )
            else:
                result = await self.bridge.send(session.text_channel_id, content)
                message_id = (
                    result.get("messageId") if isinstance(result, dict) else None
                )
                if message_id:
                    session.progress_message_id = str(message_id)
            session.progress_message_text = content
        except BridgeError:
            log.warning(
                "Could not update stream progress in channel %s",
                session.text_channel_id,
                exc_info=True,
            )

    async def _progress_reporter(self, session: StreamSession):
        while True:
            await self._update_progress_message(session)
            await asyncio.sleep(5)

    async def _start_progress_reporter(self, session: StreamSession):
        await self._update_progress_message(session)
        if session.progress_task is None or session.progress_task.done():
            session.progress_task = asyncio.create_task(
                self._progress_reporter(session),
                name=f"animax-stream-progress-{session.id[:8]}",
            )

    async def start(
        self,
        *,
        guild_id: int,
        voice_channel_id: int,
        text_channel_id: int,
        author_id: int,
        raw: str,
    ):
        try:
            request = parse_stream_request(raw)
        except StreamRequestError:
            raise
        async with self.lock:
            if self.session and self.session.task and not self.session.task.done():
                raise StreamBusy(
                    "ANIMAX already has an active Go Live stream. Use `.status` or `.stop`."
                )
            session = StreamSession(
                id=uuid.uuid4().hex,
                guild_id=int(guild_id),
                voice_channel_id=int(voice_channel_id),
                text_channel_id=int(text_channel_id),
                author_id=int(author_id),
                request=request,
            )
            self.session = session
            session.task = asyncio.create_task(
                self._run(session), name=f"animax-stream-{session.id[:8]}"
            )
        return {
            "ok": True,
            "message": (
                f"Starting `{request.query}` E{request.start}–E{request.end} "
                f"({request.language}; skip OP/ED: {'Yes' if request.skip_intro_outro else 'No'})."
            ),
        }

    @staticmethod
    def _clock(seconds: float) -> str:
        value = max(0, int(seconds))
        return f"{value // 60}:{value % 60:02d}"

    def _progress_text(self, session: StreamSession) -> str:
        episode = (
            session.current_episode
            or session.preparing_episode
            or session.prefetch_episode
            or session.playback_episode
            or session.request.start
        )
        duration = max(0.0, float(session.playback_duration or 0))
        elapsed = max(0.0, float(session.playback_elapsed or 0))
        if session.playback_state == "Streaming" and session.playback_updated_at:
            elapsed += max(0.0, time.monotonic() - session.playback_updated_at)
        if session.playback_state in {"Finding title", "Connecting", "Preparing"}:
            stage_elapsed = (
                max(0.0, time.monotonic() - session.status_started_at)
                if session.status_started_at
                else 0.0
            )
            timing = f"{self._clock(stage_elapsed)} elapsed"
        elif duration > 0:
            elapsed = min(duration, elapsed)
            timing = (
                f"{self._clock(elapsed)} / {self._clock(duration)} "
                f"({min(100, int(elapsed * 100 / duration))}%)"
            )
        else:
            timing = "0:00 / --:-- (0%)"
        content = (
            f"**{session.title or session.request.query}** | `E{episode}` | "
            f"{session.playback_state} | {timing}"
        )
        if (
            session.playback_state == "Connecting"
            and session.preparing_episode
            and session.prepare_task
            and not session.prepare_task.done()
        ):
            content += f"\nPreparing E{session.preparing_episode} in parallel"
        return content

    def status(self, guild_id: int | None = None):
        session = self.session
        if not session or not session.task or session.task.done():
            return {"ok": True, "message": "No active Go Live stream."}
        if guild_id is not None and int(guild_id) != session.guild_id:
            return {"ok": True, "message": "No active Go Live stream in this server."}
        message = self._progress_text(session)
        if (
            session.prefetch_episode
            and session.current_episode
            and session.prefetch_task
            and not session.prefetch_task.done()
        ):
            message += f"\nPreparing E{session.prefetch_episode}"
        return {
            "ok": True,
            "message": message,
            "phase": session.phase,
            "episode": session.current_episode or session.preparing_episode,
            "prefetchEpisode": session.prefetch_episode,
        }

    def _authorize_session(self, guild_id: int, author_id: int):
        session = self.session
        if not session or not session.task or session.task.done():
            raise StreamRequestError("There is no active Go Live stream.")
        if session.guild_id != int(guild_id) or session.author_id != int(author_id):
            raise StreamRequestError(
                "Only the command author can control this stream in its server."
            )
        return session

    async def skip(self, *, guild_id: int, author_id: int):
        session = self._authorize_session(guild_id, author_id)
        if session.current_episode is not None and session.series_started:
            episode = session.current_episode
            await self.bridge.series_skip(session.id, episode)
            return {
                "ok": True,
                "message": f"Skipping E{episode}; the next episode will follow in the same Go Live session.",
            }
        episode = session.preparing_episode or session.prefetch_episode
        task = session.prefetch_task or session.prepare_task
        if episode is not None and task is not None and not task.done():
            session.skip_preparing.add(episode)
            task.cancel()
            return {
                "ok": True,
                "message": f"Cancelled preparation of E{episode}; moving on to the next requested episode.",
            }
        return {"ok": True, "message": "There is no active episode to skip yet."}

    async def stop(self, *, guild_id: int, author_id: int):
        session = self._authorize_session(guild_id, author_id)
        session.stop_event.set()
        for task in (session.prepare_task, session.prefetch_task):
            if task and not task.done():
                task.cancel()
        if session.series_started:
            with contextlib.suppress(BridgeError):
                await self.bridge.series_abort(session.id)
        return {"ok": True, "message": "Stopping the stream and leaving voice."}

    async def handle_segment_event(self, body: dict):
        session = self.session
        if not session or str(body.get("seriesId")) != session.id:
            return
        if body.get("kind") != "episode":
            return
        try:
            episode = int(body.get("episode"))
        except (TypeError, ValueError):
            return
        session.episode_results[episode] = {
            "skipped": bool(body.get("skipped")),
            "error": str(body.get("error") or ""),
        }
        session.episode_events.setdefault(episode, asyncio.Event()).set()
        if episode == session.current_episode:
            session.playback_updated_at = None
            if session.episode_results[episode]["error"]:
                session.playback_state = "Failed"
            elif session.episode_results[episode]["skipped"]:
                session.playback_state = "Skipped"
            else:
                session.playback_elapsed = session.playback_duration
                session.playback_state = "Finished"

    async def handle_progress_event(self, body: dict):
        session = self.session
        if not session or str(body.get("seriesId")) != session.id:
            return
        if body.get("kind") != "episode":
            return
        try:
            episode = int(body.get("episode"))
            elapsed = float(body.get("elapsed"))
            duration = float(body.get("duration"))
        except (TypeError, ValueError):
            return
        if (
            episode != session.current_episode
            or not math.isfinite(elapsed)
            or not math.isfinite(duration)
            or elapsed < 0
            or duration <= 0
        ):
            return
        session.playback_episode = episode
        session.playback_duration = duration
        session.playback_elapsed = min(elapsed, duration)
        session.playback_updated_at = time.monotonic()
        session.playback_state = "Streaming"

    async def handle_series_event(self, body: dict):
        session = self.session
        if not session or str(body.get("seriesId")) != session.id:
            return
        session.node_error = str(body.get("error") or "")
        if session.current_episode is not None:
            session.episode_results[session.current_episode] = {
                "skipped": False,
                "error": session.node_error
                or "Go Live ended before the current episode finished.",
            }
            session.episode_events.setdefault(
                session.current_episode, asyncio.Event()
            ).set()
        session.series_finished.set()

    async def _await_prepare(
        self, session: StreamSession, episode: int, task: asyncio.Task
    ):
        try:
            return await task
        except asyncio.CancelledError:
            if not self.settings.keep_stream_cache:
                shutil.rmtree(self._episode_work(session, episode), ignore_errors=True)
            if session.stop_event.is_set():
                return None
            if episode in session.skip_preparing:
                session.skip_preparing.discard(episode)
                await self._send(
                    session, f"Preparation of E{episode} was cancelled; advancing."
                )
                return SKIPPED
            raise
        except Exception:
            if not self.settings.keep_stream_cache:
                shutil.rmtree(self._episode_work(session, episode), ignore_errors=True)
            raise
        finally:
            if session.prepare_task is task:
                session.prepare_task = None
            if session.prefetch_task is task:
                session.prefetch_task = None
                session.prefetch_started_at = None
            if session.preparing_episode == episode:
                session.preparing_episode = None
            if session.prefetch_episode == episode:
                session.prefetch_episode = None

    async def _wait_episode(self, session: StreamSession, episode: int):
        completed = session.episode_events.setdefault(episode, asyncio.Event())
        finished = asyncio.create_task(completed.wait())
        stopped = asyncio.create_task(session.stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {finished, stopped}, return_when=asyncio.FIRST_COMPLETED
            )
            if stopped in done and session.stop_event.is_set():
                return {"stopped": True}
            await finished
            result = session.episode_results.get(episode, {})
            if result.get("error"):
                raise MediaError(f"Go Live segment failed: {result['error']}")
            return result
        finally:
            for task in (finished, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(finished, stopped, return_exceptions=True)

    async def _run(self, session: StreamSession):
        terminal = "failed"
        error_text = ""
        prepared: PreparedEpisode | None = None
        current_episode = session.request.start
        try:
            session.phase = "Finding the best title match"
            session.playback_episode = session.request.start
            session.playback_state = "Finding title"
            session.status_started_at = time.monotonic()
            await self._start_progress_reporter(session)
            mal_id, title, score = await resolve_anime(
                session.request.query, self.root / "title_overrides.json"
            )
            if session.stop_event.is_set():
                terminal = "stopped"
                return
            session.mal_id, session.title = mal_id, title
            session.phase = "Connecting to the voice channel"
            session.playback_state = "Connecting"
            session.status_started_at = time.monotonic()
            await self._update_progress_message(session)
            if provisional_title(title, mal_id):
                title_message = (
                    f"Title lookup is unavailable; continuing with MAL ID {mal_id} "
                    f"and placeholder title `{title}`. Connecting the streaming account to voice…"
                )
            else:
                title_message = (
                    f"Matched **{title}** (MAL {mal_id}, match {score}%). "
                    "Connecting the streaming account to your current voice channel…"
                )
            await self._send(session, title_message)
            session.card_directory = (
                self.root / "downloads" / ".animax_cards" / session.id
            )
            episode = session.request.start
            session.phase = f"Preparing E{episode} and connecting voice"
            session.playback_episode = episode
            session.playback_state = "Connecting"
            session.playback_duration = 0.0
            session.playback_elapsed = 0.0
            session.playback_updated_at = None
            session.status_started_at = time.monotonic()
            session.preparing_episode = episode
            await self._update_progress_message(session)
            first_task = asyncio.create_task(
                self._prepare_episode(session, episode),
                name=f"animax-first-episode-e{episode}",
            )
            session.prepare_task = first_task
            await self.bridge.join(session.guild_id, session.voice_channel_id)
            if not first_task.done() and not session.stop_event.is_set():
                session.phase = f"Preparing E{episode}"
                session.playback_state = "Preparing"
                session.status_started_at = time.monotonic()
                await self._update_progress_message(session)
            prepared = await self._await_prepare(session, episode, first_task)
            if prepared is SKIPPED:
                session.playback_state = "Skipped"
                session.status_started_at = None
                await self._update_progress_message(session)
                episode += 1
                prepared = None
            elif prepared is not None:
                if session.stop_event.is_set():
                    if not self.settings.keep_stream_cache:
                        shutil.rmtree(prepared.work, ignore_errors=True)
                    prepared = None
                else:
                    session.playback_duration = prepared.duration
                    session.playback_elapsed = 0.0
                    session.playback_state = "Starting"
                    session.status_started_at = None
                    await self._update_progress_message(session)

            while episode <= session.request.end and not session.stop_event.is_set():
                if prepared is None:
                    session.phase = f"Preparing E{episode}"
                    session.playback_episode = episode
                    session.playback_state = "Preparing"
                    session.playback_duration = 0.0
                    session.playback_elapsed = 0.0
                    session.playback_updated_at = None
                    session.status_started_at = time.monotonic()
                    session.preparing_episode = episode
                    await self._update_progress_message(session)
                    task = asyncio.create_task(
                        self._prepare_episode(session, episode),
                        name=f"animax-prepare-e{episode}",
                    )
                    session.prepare_task = task
                    prepared = await self._await_prepare(session, episode, task)
                    if prepared is SKIPPED:
                        session.playback_state = "Skipped"
                        session.status_started_at = None
                        await self._update_progress_message(session)
                        episode += 1
                        continue
                    if prepared is None or session.stop_event.is_set():
                        break
                    session.playback_duration = prepared.duration
                    session.playback_elapsed = 0.0
                    session.playback_state = "Starting"
                    session.status_started_at = None
                    await self._update_progress_message(session)
                current_episode = episode
                card = await self._make_card(session, episode)
                if session.stop_event.is_set():
                    break
                if not session.series_started:
                    await self.bridge.series_start(
                        session.id,
                        session.guild_id,
                        session.voice_channel_id,
                        bitrate=self.settings.stream_bitrate_kbps,
                        threads=self.settings.encode_threads,
                        preset=STREAM_ENCODER_PRESET,
                    )
                    session.series_started = True
                session.current_episode = episode
                session.playback_episode = episode
                session.playback_state = "Starting"
                session.playback_elapsed = 0.0
                session.playback_duration = prepared.duration
                session.playback_updated_at = None
                session.episode_events[episode] = asyncio.Event()
                session.episode_results.pop(episode, None)
                await self.bridge.series_add(
                    session.id,
                    episode,
                    "card",
                    str(card.resolve()),
                    2.8,
                )
                await self.bridge.series_add(
                    session.id,
                    episode,
                    "episode",
                    str(prepared.path.resolve()),
                    prepared.duration,
                )
                await self._start_progress_reporter(session)

                next_task = None
                next_episode = episode + 1
                if next_episode <= session.request.end:
                    next_task = asyncio.create_task(
                        self._prepare_episode(session, next_episode),
                        name=f"animax-prefetch-e{next_episode}",
                    )
                    next_task.add_done_callback(self._consume_task_exception)
                    session.prefetch_task = next_task
                    session.prefetch_episode = next_episode
                    session.prefetch_started_at = time.monotonic()
                    if session.stop_event.is_set():
                        break
                    session.phase = f"Streaming E{episode}; preparing E{next_episode}"

                result = await self._wait_episode(session, episode)
                if result.get("stopped") or session.stop_event.is_set():
                    break
                await self._update_progress_message(session)
                session.current_episode = None
                if not self.settings.keep_stream_cache:
                    shutil.rmtree(prepared.work, ignore_errors=True)
                prepared = None

                if next_task:
                    session.phase = f"Waiting for E{next_episode} to finish preparing"
                    if not next_task.done():
                        session.playback_episode = next_episode
                        session.playback_state = "Preparing"
                        session.playback_duration = 0.0
                        session.playback_elapsed = 0.0
                        session.playback_updated_at = None
                        session.status_started_at = (
                            session.prefetch_started_at or time.monotonic()
                        )
                        await self._update_progress_message(session)
                    next_prepared = await self._await_prepare(
                        session, next_episode, next_task
                    )
                    if next_prepared is SKIPPED:
                        session.playback_episode = next_episode
                        session.playback_state = "Skipped"
                        session.status_started_at = None
                        await self._update_progress_message(session)
                        episode = next_episode + 1
                        continue
                    if next_prepared is None:
                        break
                    if session.stop_event.is_set():
                        if not self.settings.keep_stream_cache:
                            shutil.rmtree(next_prepared.work, ignore_errors=True)
                        break
                    prepared = next_prepared
                    episode = next_episode
                    session.playback_episode = next_episode
                    session.playback_state = "Starting"
                    session.playback_duration = prepared.duration
                    session.playback_elapsed = 0.0
                    session.playback_updated_at = None
                    session.status_started_at = None
                    await self._update_progress_message(session)
                else:
                    episode += 1

            if session.stop_event.is_set():
                terminal = "stopped"
            else:
                terminal = "finished"
        except asyncio.CancelledError:
            session.stop_event.set()
            terminal = "stopped"
        except (TitleError, BridgeError, MediaError, OSError, ValueError) as exc:
            error_text = str(exc)
            log.exception("Stream session %s failed", session.id)
            terminal = "failed"
        except Exception as exc:
            error_text = str(exc)
            log.exception("Unexpected stream session failure %s", session.id)
            terminal = "failed"
        finally:
            pending_episodes = {
                episode
                for episode in (session.preparing_episode, session.prefetch_episode)
                if episode is not None
            }
            for task in (session.prepare_task, session.prefetch_task):
                if task and not task.done():
                    task.cancel()
            pending = [t for t in (session.prepare_task, session.prefetch_task) if t]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not self.settings.keep_stream_cache:
                for episode in pending_episodes:
                    shutil.rmtree(
                        self._episode_work(session, episode), ignore_errors=True
                    )
                if prepared:
                    shutil.rmtree(prepared.work, ignore_errors=True)
            if session.series_started:
                if terminal in {"stopped", "failed"}:
                    with contextlib.suppress(BridgeError):
                        await self.bridge.series_abort(session.id)
                else:
                    with contextlib.suppress(BridgeError):
                        await self.bridge.series_end(session.id)
                    try:
                        await asyncio.wait_for(
                            session.series_finished.wait(), timeout=30
                        )
                    except asyncio.TimeoutError:
                        terminal = "failed"
                        error_text = (
                            error_text
                            or "Go Live sidecar did not finish the series cleanly."
                        )
                        with contextlib.suppress(BridgeError):
                            await self.bridge.series_abort(session.id)
                if session.node_error and terminal == "finished":
                    terminal = "failed"
                    error_text = session.node_error[:1000]
            with contextlib.suppress(BridgeError):
                await self.bridge.leave(session.guild_id)
            if session.progress_task and not session.progress_task.done():
                session.progress_task.cancel()
                await asyncio.gather(session.progress_task, return_exceptions=True)
            session.progress_task = None
            if session.card_directory:
                shutil.rmtree(session.card_directory, ignore_errors=True)
            session.current_episode = None
            session.preparing_episode = None
            session.prefetch_episode = None
            session.phase = {
                "finished": "Finished",
                "stopped": "Stopped",
                "failed": "Failed",
            }[terminal]
            session.playback_state = session.phase
            session.playback_updated_at = None
            if session.progress_message_text:
                await self._update_progress_message(session)
            if terminal == "failed":
                await self._send(
                    session,
                    f"Stream stopped: {error_text[:900] or 'see logs/bot.log for details.'}",
                )
            elif terminal == "stopped":
                await self._send(session, "Stream stopped; voice connection closed.")
            else:
                await self._send(
                    session,
                    f"Finished streaming **{session.title}** through Episode {current_episode}.",
                )
            log.info(
                "STREAM_END id=%s status=%s mal=%s title=%s episode=%s",
                session.id,
                terminal,
                session.mal_id,
                session.title,
                current_episode,
            )

    @staticmethod
    def _consume_task_exception(task: asyncio.Task):
        if not task.cancelled():
            with contextlib.suppress(Exception):
                task.exception()

    async def _make_card(self, session: StreamSession, episode: int):
        font_name = FONT_FILES.get(
            self.settings.caption_font, "LiberationSans-Regular.ttf"
        )
        font = self.root / "assets" / "fonts" / font_name
        if not font.exists():
            raise MediaError(f"Selected title-card font is missing: {font.name}")
        return await create_title_card(
            session.card_directory,
            session.title,
            episode,
            font=str(font.resolve()),
            timeout=45,
            max_bytes=self.settings.max_temp_bytes,
            disk_root=self.disk_root,
        )

    def _episode_work(self, session: StreamSession, episode: int) -> Path:
        if session.mal_id is None:
            raise MediaError(
                "Cannot prepare an episode before anime metadata is resolved."
            )
        skip_tag = "skip" if session.request.skip_intro_outro else "full"
        return (
            self.cache_root
            / f"mal-{session.mal_id}"
            / f"{session.request.language}-e{episode:04d}-{skip_tag}"
        )

    async def _prepare_episode(self, session: StreamSession, episode: int):
        work = self._episode_work(session, episode)
        work.mkdir(parents=True, exist_ok=True)
        cfg = self.settings
        cached = await cached_video(
            work,
            session.request.language,
            1080,
            "burn",
            encoder_preset=STREAM_ENCODER_PRESET,
            target_mb=cfg.target_mb,
            video_crf=cfg.video_crf,
            compression_mode=cfg.compression_mode,
            compress_all=cfg.compress_all,
            lossless=cfg.lossless,
            caption_font=cfg.caption_font,
            frame_rate=cfg.frame_rate,
            skip_intro_outro=session.request.skip_intro_outro,
        )
        if cached:
            path, note = cached
            if note and not session.skip_notice_sent:
                session.skip_notice_sent = True
                await self._send(session, note[:900])
            info = await probe(path)
            return PreparedEpisode(
                episode, work, path, float(info["format"]["duration"]), note
            )

        async with Resolver(
            timeout=cfg.resolve_timeout, headless=cfg.headless
        ) as resolver:
            async with resolve_with_fallback(
                resolver,
                session.mal_id,
                session.request.language,
                episode,
                allow_fallback=cfg.dub_fallback,
            ) as (stream, actual_language, fallback_reason):
                if fallback_reason and not session.fallback_notice_sent:
                    session.fallback_notice_sent = True
                    await self._send(
                        session,
                        f"Dub or English dub captions were unavailable for E{episode}; falling back to sub. This notice is shown once per stream.",
                    )
                has_caption = choose_caption(stream.tracks, actual_language) is not None
                if not has_caption and not session.caption_notice_sent:
                    session.caption_notice_sent = True
                    await self._send(
                        session,
                        f"No usable English captions were available for E{episode}; this episode will play without captions.",
                    )
                try:
                    transfer = await stream.detach(
                        actual_language, with_captions=has_caption
                    )
                except CaptionUnavailable:
                    if not session.caption_notice_sent:
                        session.caption_notice_sent = True
                        await self._send(
                            session,
                            f"The English caption file was unavailable for E{episode}; this episode will play without captions.",
                        )
                    transfer = await stream.detach(actual_language, with_captions=False)
                path, note = await download(
                    transfer,
                    work,
                    actual_language,
                    quality=1080,
                    subtitle_mode="burn",
                    max_bytes=cfg.max_temp_bytes,
                    timeout=cfg.operation_timeout,
                    request_interval=cfg.request_interval,
                    encoder_preset=STREAM_ENCODER_PRESET,
                    encode_threads=cfg.encode_threads,
                    target_mb=cfg.target_mb,
                    video_crf=cfg.video_crf,
                    compression_mode=cfg.compression_mode,
                    compress_all=cfg.compress_all,
                    lossless=cfg.lossless,
                    caption_font=cfg.caption_font,
                    frame_rate=cfg.frame_rate,
                    disk_root=self.disk_root,
                    skip_intro_outro=session.request.skip_intro_outro,
                    mal_id=session.mal_id,
                    episode=episode,
                )
                if note and not session.skip_notice_sent:
                    session.skip_notice_sent = True
                    await self._send(session, note[:900])
        info = await probe(path)
        duration = float(info["format"]["duration"])
        return PreparedEpisode(episode, work, path, duration, note)
