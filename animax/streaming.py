"""Single-session Go Live playback from Discord attachments journaled in anime.json."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import discord

from .bridge import BridgeError, StreamBridge
from .encoding import FONT_FILES
from .metadata import TitleError, clean_title, select_best_anime
from .settings import Settings
from .stream_media import (
    MediaError,
    attachment_extension,
    download_discord_attachment,
    ensure_playable,
    probe,
)
from .titlecard import create_title_card

log = logging.getLogger("animax.streaming")
ROOT = Path(__file__).resolve().parent.parent
SKIPPED = object()
STREAM_ENCODER_PRESET = "ultrafast"


class StreamRequestError(ValueError):
    pass


class StreamBusy(RuntimeError):
    pass


def resolve_tracked_anime(query: str, library_data: dict):
    """Resolve only against anime.json; streaming never searches external media sites."""
    query = " ".join(str(query or "").split()).strip()
    if not query:
        raise TitleError("Enter a MAL ID or anime title tracked in anime.json.")
    anime = library_data.get("anime", {}) if isinstance(library_data, dict) else {}
    if not isinstance(anime, dict) or not anime:
        raise TitleError("anime.json has no uploaded anime to stream yet.")

    id_query = re.sub(r"^mal\s+", "", query, flags=re.IGNORECASE).strip()
    if id_query.isdecimal():
        mal_id = int(id_query)
        if mal_id < 1:
            raise TitleError("MAL ID must be positive.")
        row = anime.get(str(mal_id))
        if not isinstance(row, dict):
            raise TitleError(f"MAL {mal_id} is not tracked in anime.json.")
        raw_title = row.get("title") or f"MAL {mal_id}"
        try:
            title = clean_title(raw_title)
        except TitleError:
            title = f"MAL {mal_id}"
        return mal_id, title, 100

    candidates = []
    for mal_key, row in anime.items():
        try:
            mal_id = int(mal_key)
        except (TypeError, ValueError):
            continue
        if mal_id < 1 or not isinstance(row, dict):
            continue
        title = row.get("title")
        if isinstance(title, str) and title.strip():
            candidates.append({"idMal": mal_id, "title": title})
    try:
        mal_id, title, score = select_best_anime(query, candidates)
    except TitleError as exc:
        raise TitleError(
            f"No close title in anime.json matched `{query}`. Use a tracked MAL ID or run `.history` first."
        ) from exc
    return mal_id, title, score


def select_uploaded_episode(
    library_data: dict,
    mal_id: int,
    episode: int,
    language: str | None = None,
    *,
    preferred_guild_id: int | str | None = None,
    preferred_account_id: int | str | None = None,
) -> dict:
    """Find the newest confirmed Discord message for one tracked episode/language."""
    anime = library_data.get("anime", {}) if isinstance(library_data, dict) else {}
    anime_row = anime.get(str(mal_id), {}) if isinstance(anime, dict) else {}
    episodes = anime_row.get("episodes", {}) if isinstance(anime_row, dict) else {}
    if not isinstance(episodes, dict):
        episodes = {}

    languages = [language] if language in {"sub", "dub"} else ["dub", "sub"]
    candidates = []
    for track in languages:
        episode_row = episodes.get(track, {})
        if not isinstance(episode_row, dict):
            continue
        entry = episode_row.get(str(episode), {})
        if not isinstance(entry, dict):
            continue
        deliveries = entry.get("deliveries", {})
        if not isinstance(deliveries, dict):
            continue
        for delivery_key, delivery in deliveries.items():
            if not isinstance(delivery, dict):
                continue
            key_parts = str(delivery_key).split(":", 1)
            account_id = str(
                delivery.get("account_id") or (key_parts[0] if key_parts else "")
            )
            channel_id = str(
                delivery.get("channel_id")
                or (key_parts[1] if len(key_parts) > 1 else "")
            )
            messages = delivery.get("messages")
            if not isinstance(messages, list):
                messages = []
            if not messages:
                legacy_ids = delivery.get("message_ids") or []
                if not legacy_ids and delivery.get("message_id"):
                    legacy_ids = [delivery["message_id"]]
                messages = [{"message_id": value} for value in legacy_ids]
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_id = str(message.get("message_id") or "")
                message_channel = str(message.get("channel_id") or channel_id)
                if not message_id.isdecimal() or not message_channel.isdecimal():
                    continue
                message_account = str(message.get("account_id") or account_id)
                guild_id = str(
                    message.get("guild_id") or delivery.get("guild_id") or ""
                )
                candidates.append(
                    {
                        "mal_id": int(mal_id),
                        "episode": int(episode),
                        "language": track,
                        "account_id": message_account,
                        "channel_id": message_channel,
                        "message_id": message_id,
                        "guild_id": guild_id,
                        "uploaded_at": str(
                            message.get("uploaded_at")
                            or delivery.get("updated_at")
                            or ""
                        ),
                        "jump_url": message.get("jump_url"),
                        "size_bytes": message.get("size_bytes")
                        or delivery.get("size_bytes"),
                    }
                )

    if not candidates:
        if language:
            raise MediaError(
                f"No uploaded {language} attachment for MAL {mal_id} E{episode} is recorded in anime.json."
            )
        raise MediaError(
            f"No uploaded sub or dub attachment for MAL {mal_id} E{episode} is recorded in anime.json."
        )

    preferred_guild = str(preferred_guild_id or "")
    preferred_account = str(preferred_account_id or "")
    language_order = {"dub": 0, "sub": 1}
    candidates.sort(
        key=lambda item: (
            bool(preferred_guild and item["guild_id"] == preferred_guild),
            bool(preferred_account and item["account_id"] == preferred_account),
            -language_order.get(item["language"], 2) if language is None else 0,
            item["uploaded_at"],
        ),
        reverse=True,
    )
    return candidates[0]


@dataclass(frozen=True)
class StreamRequest:
    query: str
    start: int
    end: int
    language: str | None = None


def parse_stream_request(raw: str) -> StreamRequest:
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise StreamRequestError(f"Could not parse the command: {exc}") from None
    if len(tokens) < 3:
        raise StreamRequestError(
            "Usage: `.stream <MAL ID or anime name> <start episode> <end episode> [sub|dub]`."
        )

    language = None
    seen_language = False
    if tokens and tokens[-1].casefold() in {"yes", "no"}:
        raise StreamRequestError(
            "Use only an optional `sub` or `dub` language after the episode range."
        )
    while tokens and tokens[-1].casefold() in {"sub", "dub"}:
        option = tokens.pop().casefold()
        if seen_language:
            raise StreamRequestError("Choose only one audio language: sub or dub.")
        language = option
        seen_language = True
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
        raise StreamRequestError("Enter a MAL ID or anime name tracked in anime.json.")
    if start < 1 or end < start or end - start + 1 > 100:
        raise StreamRequestError(
            "Use a positive inclusive range of no more than 100 episodes."
        )
    return StreamRequest(query, start, end, language)


@dataclass
class PreparedEpisode:
    episode: int
    work: Path
    path: Path
    duration: float
    note: str | None = None
    language: str | None = None
    message_id: str | None = None


@dataclass
class StreamSession:
    id: str
    guild_id: int
    voice_channel_id: int
    text_channel_id: int
    author_id: int
    request: StreamRequest
    phase: str = "Finding uploaded anime"
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
    card_directory: Path | None = None
    playback_episode: int | None = None
    playback_state: str = "Starting"
    playback_elapsed: float = 0.0
    playback_duration: float = 0.0
    playback_updated_at: float | None = None
    status_started_at: float | None = None
    progress_message_id: str | None = None
    progress_message_text: str = ""
    progress_task: asyncio.Task | None = None
    progress_message: object | None = None
    selected_languages: dict[int, str] = field(default_factory=dict)
    download_episode: int | None = None
    download_stage: str = ""
    download_received: int = 0
    download_total: int = 0
    download_started_at: float | None = None
    download_last_log_percent: int = -10
    terminal_detail: str = ""
    started_at: float | None = None
    last_terminal_progress: int = -1


class StreamManager:
    def __init__(
        self,
        settings: Settings,
        bridge: StreamBridge,
        root: Path = ROOT,
        *,
        library=None,
        attachment_lookup=None,
        ui=None,
        command_account_id: int | str | None = None,
    ):
        self.settings = settings
        self.bridge = bridge
        self.root = Path(root)
        self.cache_root = self.root / "downloads" / "stream-cache"
        self.disk_root = self.root / "downloads"
        self.library = library
        self.attachment_lookup = attachment_lookup
        self.ui = ui
        self.command_account_id = (
            str(command_account_id) if command_account_id else None
        )
        self.session: StreamSession | None = None
        self.lock = asyncio.Lock()

    def _event(self, message: str, style="cyan"):
        log.info("STREAM %s", message)
        if self.ui is not None and hasattr(self.ui, "event"):
            self.ui.event(f"STREAM | {message}", style)
        else:
            print(f"[{time.strftime('%H:%M:%S')}] STREAM | {message}", flush=True)

    async def _send(self, session: StreamSession, text: str):
        """Fallback for secondary notices; the main status is edited in place."""
        try:
            return await self.bridge.send(session.text_channel_id, text)
        except BridgeError:
            self._event(
                f"Could not send a secondary stream notice: {text[:120]}", "yellow"
            )
            log.warning(
                "Could not send stream status to channel %s",
                session.text_channel_id,
                exc_info=True,
            )
            return None

    async def _update_progress_message(self, session: StreamSession):
        content = self._progress_text(session)
        if content == session.progress_message_text:
            return
        try:
            if session.progress_message is not None:
                await session.progress_message.edit(content=content)
                if getattr(session.progress_message, "id", None):
                    session.progress_message_id = str(session.progress_message.id)
            elif session.progress_message_id:
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
        except Exception as exc:
            log.warning(
                "Could not update stream progress in channel %s",
                session.text_channel_id,
                exc_info=True,
            )
            self._event(
                f"Progress-message edit failed ({type(exc).__name__}: {str(exc)[:180]}).",
                "yellow",
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
        progress_message=None,
    ):
        request = parse_stream_request(raw)
        library_data = getattr(self.library, "data", None)
        if library_data is None:
            raise TitleError(
                "The main Discord client did not provide anime.json for streaming."
            )
        mal_id, title, score = resolve_tracked_anime(request.query, library_data)
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
                mal_id=mal_id,
                title=title,
                playback_episode=request.start,
                progress_message=progress_message,
                started_at=time.monotonic(),
                status_started_at=time.monotonic(),
            )
            self.session = session
            session.task = asyncio.create_task(
                self._run(session), name=f"animax-stream-{session.id[:8]}"
            )
        language_text = (
            f" ({request.language})" if request.language else " (auto language)"
        )
        self._event(
            f"START id={session.id[:8]} MAL={mal_id} title={title!r} "
            f"episodes={request.start}-{request.end}{language_text} "
            f"guild={guild_id} voice={voice_channel_id}",
        )
        return {
            "ok": True,
            "message": f"Starting **{title}** E{request.start}–E{request.end} from anime.json.",
        }

    @staticmethod
    def _clock(seconds: float) -> str:
        value = max(0, int(seconds))
        return f"{value // 60}:{value % 60:02d}"

    @staticmethod
    def _size_text(value: int) -> str:
        amount = max(0, int(value))
        if amount < 1_000_000_000:
            return f"{amount / 1_000_000:.1f} MB"
        return f"{amount / 1_000_000_000:.2f} GB"

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
        if duration > 0:
            elapsed = min(duration, elapsed)
            timing = (
                f"{self._clock(elapsed)} / {self._clock(duration)} "
                f"({min(100, int(elapsed * 100 / duration))}%)"
            )
        else:
            stage_elapsed = (
                max(0.0, time.monotonic() - session.status_started_at)
                if session.status_started_at
                else 0.0
            )
            timing = f"{self._clock(stage_elapsed)} / --:-- (0%)"

        title = discord.utils.escape_markdown(session.title or session.request.query)
        state = session.playback_state
        track = session.selected_languages.get(episode)
        if track:
            state += f" ({track.upper()})"
        content = (
            f"**{title}** | `E{episode}/{session.request.end}` | {state} | {timing}"
        )

        if session.download_episode is not None:
            download_episode = session.download_episode
            stage = session.download_stage or "Looking up Discord upload"
            if stage == "Downloading" and session.download_total > 0:
                percent = min(
                    100,
                    int(session.download_received * 100 / session.download_total),
                )
                detail = (
                    f"{self._size_text(session.download_received)} / "
                    f"{self._size_text(session.download_total)} ({percent}%)"
                )
            elif stage == "Downloading":
                detail = f"{self._size_text(session.download_received)} downloaded"
            else:
                detail = stage
            if download_episode == episode and session.current_episode is None:
                content = (
                    f"**{title}** | `E{episode}/{session.request.end}` | "
                    f"{stage} | {timing}"
                )
                if session.download_total > 0:
                    percent = min(
                        100,
                        int(session.download_received * 100 / session.download_total),
                    )
                    content += f" · {self._size_text(session.download_received)} / {self._size_text(session.download_total)} ({percent}%)"
            else:
                content += f"\n↳ E{download_episode} download | {detail}"

        if session.terminal_detail:
            content += f"\n{session.terminal_detail[:500]}"
        return content[:1900]

    def status(self, guild_id: int | None = None):
        session = self.session
        if not session or not session.task or session.task.done():
            return {"ok": True, "message": "No active Go Live stream."}
        if guild_id is not None and int(guild_id) != session.guild_id:
            return {"ok": True, "message": "No active Go Live stream in this server."}
        return {
            "ok": True,
            "message": self._progress_text(session),
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
            self._event(f"SKIP id={session.id[:8]} episode={episode}")
            return {
                "ok": True,
                "message": f"Skipping E{episode}; the next episode will follow in the same Go Live session.",
            }
        episode = session.preparing_episode or session.prefetch_episode
        task = session.prefetch_task or session.prepare_task
        if episode is not None and task is not None and not task.done():
            session.skip_preparing.add(episode)
            task.cancel()
            self._event(f"SKIP_PREPARATION id={session.id[:8]} episode={episode}")
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
        self._event(f"STOP_REQUESTED id={session.id[:8]}")
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
                self._event(
                    f"PLAYBACK_ERROR id={session.id[:8]} episode={episode}: "
                    f"{session.episode_results[episode]['error'][:240]}",
                    "red",
                )
            elif session.episode_results[episode]["skipped"]:
                session.playback_state = "Skipped"
            else:
                session.playback_elapsed = session.playback_duration
                session.playback_state = "Finished"
                self._event(f"EPISODE_FINISHED id={session.id[:8]} episode={episode}")

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
        if session.playback_state != "Streaming":
            self._event(f"PLAYBACK_STARTED id={session.id[:8]} episode={episode}")
        session.playback_state = "Streaming"
        bucket = int(elapsed // 30)
        if bucket > session.last_terminal_progress:
            session.last_terminal_progress = bucket
            self._event(
                f"PLAYBACK_PROGRESS id={session.id[:8]} episode={episode} "
                f"{self._clock(elapsed)}/{self._clock(duration)} "
                f"{min(100, int(elapsed * 100 / duration))}%"
            )

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
        current_episode = session.request.start - 1
        join_task: asyncio.Task | None = None
        try:
            session.phase = "Finding uploaded anime in anime.json"
            session.playback_episode = session.request.start
            session.playback_state = "Looking up Discord upload"
            session.status_started_at = time.monotonic()
            await self._start_progress_reporter(session)
            self._event(
                f"LOOKUP_LOCAL id={session.id[:8]} MAL={session.mal_id} "
                f"range={session.request.start}-{session.request.end}"
            )
            session.card_directory = (
                self.root / "downloads" / ".animax_cards" / session.id
            )
            episode = session.request.start
            session.phase = f"Preparing E{episode} and joining voice"
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
            self._event(
                f"VOICE_CONNECT id={session.id[:8]} guild={session.guild_id} "
                f"channel={session.voice_channel_id}"
            )
            join_task = asyncio.create_task(
                self.bridge.join(session.guild_id, session.voice_channel_id),
                name=f"animax-stream-join-{session.id[:8]}",
            )
            done, _ = await asyncio.wait(
                {first_task, join_task}, return_when=asyncio.FIRST_COMPLETED
            )
            prepared_first = None
            first_consumed = False
            if first_task in done:
                prepared_first = await self._await_prepare(session, episode, first_task)
                first_consumed = True
            await join_task
            join_task = None
            self._event(f"VOICE_CONNECTED id={session.id[:8]}", "green")
            if not first_consumed:
                prepared_first = await self._await_prepare(session, episode, first_task)
            prepared = prepared_first

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
                    session.phase = f"Preparing E{episode} from anime.json"
                    session.playback_episode = episode
                    session.playback_state = "Looking up Discord upload"
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
                    self._event(f"SERIES_START id={session.id[:8]} episode={episode}")
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
                self._event(
                    f"SEGMENT_QUEUED id={session.id[:8]} episode={episode} "
                    f"language={prepared.language or 'unknown'} duration={self._clock(prepared.duration)} "
                    f"message={prepared.message_id or 'cached'}"
                )

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
                    session.phase = f"Streaming E{episode}; downloading E{next_episode}"

                result = await self._wait_episode(session, episode)
                if result.get("stopped") or session.stop_event.is_set():
                    break
                await self._update_progress_message(session)
                session.current_episode = None
                if not self.settings.keep_stream_cache:
                    shutil.rmtree(prepared.work, ignore_errors=True)
                prepared = None

                if next_task:
                    session.phase = f"Waiting for E{next_episode} to finish downloading"
                    if not next_task.done():
                        session.playback_episode = next_episode
                        session.playback_state = "Looking up Discord upload"
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
            self._event(
                f"FAILED id={session.id[:8]} type={type(exc).__name__}: {error_text[:500]}",
                "red",
            )
            terminal = "failed"
        except Exception as exc:
            error_text = str(exc)
            log.exception("Unexpected stream session failure %s", session.id)
            self._event(
                f"FAILED id={session.id[:8]} type={type(exc).__name__}: {error_text[:500]}",
                "red",
            )
            terminal = "failed"
        finally:
            if join_task and not join_task.done():
                join_task.cancel()
                await asyncio.gather(join_task, return_exceptions=True)
            pending_episodes = {
                episode_id
                for episode_id in (session.preparing_episode, session.prefetch_episode)
                if episode_id is not None
            }
            for task in (session.prepare_task, session.prefetch_task):
                if task and not task.done():
                    task.cancel()
            pending = [
                task for task in (session.prepare_task, session.prefetch_task) if task
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not self.settings.keep_stream_cache:
                for episode_id in pending_episodes:
                    shutil.rmtree(
                        self._episode_work(session, episode_id), ignore_errors=True
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
            last_episode = (
                session.current_episode
                or session.preparing_episode
                or session.playback_episode
                or current_episode
                or session.request.start
            )
            session.current_episode = None
            session.preparing_episode = None
            session.prefetch_episode = None
            session.download_episode = None
            session.phase = {
                "finished": "Finished",
                "stopped": "Stopped",
                "failed": "Failed",
            }[terminal]
            session.playback_episode = last_episode
            session.playback_state = session.phase
            session.playback_updated_at = None
            session.terminal_detail = error_text[:700] if terminal == "failed" else ""
            if session.progress_message_text or session.progress_message is not None:
                await self._update_progress_message(session)
            self._event(
                f"END id={session.id[:8]} status={terminal} MAL={session.mal_id} "
                f"episode={last_episode} error={error_text[:240]!r}",
                "green"
                if terminal == "finished"
                else "yellow"
                if terminal == "stopped"
                else "red",
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

    def _episode_work(
        self, session: StreamSession, episode: int, language: str | None = None
    ) -> Path:
        if session.mal_id is None:
            raise MediaError(
                "Cannot prepare an episode before anime metadata is resolved."
            )
        track = (
            language
            or session.selected_languages.get(episode)
            or session.request.language
            or "auto"
        )
        return (
            self.cache_root
            / f"mal-{session.mal_id}"
            / f"{track}-e{episode:04d}-discord"
        )

    @staticmethod
    def _read_cache_marker(work: Path, record: dict):
        marker = work / "ready.json"
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if (
                data.get("version") != 1
                or str(data.get("message_id")) != str(record["message_id"])
                or str(data.get("channel_id")) != str(record["channel_id"])
            ):
                return None
            path = work / str(data["file"])
            if (
                path.is_symlink()
                or path.parent.resolve() != work.resolve()
                or not path.is_file()
            ):
                return None
            return path
        except (OSError, ValueError, KeyError, TypeError):
            return None

    async def _prepare_episode(self, session: StreamSession, episode: int):
        if session.mal_id is None:
            raise MediaError(
                "Cannot prepare an episode before anime metadata is resolved."
            )
        library_data = getattr(self.library, "data", None)
        if library_data is None:
            raise MediaError("anime.json is unavailable to the stream manager.")
        record = select_uploaded_episode(
            library_data,
            session.mal_id,
            episode,
            session.request.language,
            preferred_guild_id=session.guild_id,
            preferred_account_id=self.command_account_id,
        )
        language = record["language"]
        session.selected_languages[episode] = language
        work = self._episode_work(session, episode, language)
        work.mkdir(parents=True, exist_ok=True, mode=0o700)
        session.download_episode = episode
        session.download_stage = "Checking saved Discord message"
        session.download_received = 0
        session.download_total = int(record.get("size_bytes") or 0)
        session.download_started_at = time.monotonic()
        session.download_last_log_percent = -10
        foreground = (
            session.current_episode is None or session.current_episode == episode
        )
        if foreground:
            session.playback_episode = episode
            session.playback_state = "Looking up Discord upload"
            session.status_started_at = session.status_started_at or time.monotonic()
        self._event(
            f"EPISODE_LOOKUP id={session.id[:8]} MAL={session.mal_id} E{episode} "
            f"language={language} channel={record['channel_id']} message={record['message_id']}"
        )
        cached = self._read_cache_marker(work, record)
        if cached:
            try:
                info = await probe(cached)
                duration = float(info["format"]["duration"])
                session.download_episode = None
                session.download_stage = ""
                self._event(
                    f"CACHE_HIT id={session.id[:8]} E{episode} message={record['message_id']} "
                    f"file={cached.name} duration={self._clock(duration)}"
                )
                return PreparedEpisode(
                    episode,
                    work,
                    cached,
                    duration,
                    language=language,
                    message_id=record["message_id"],
                )
            except (MediaError, OSError, ValueError):
                self._event(
                    f"CACHE_INVALID id={session.id[:8]} E{episode}; redownloading",
                    "yellow",
                )

        if self.attachment_lookup is None:
            raise MediaError(
                "The main Discord account cannot fetch saved episode attachments."
            )
        try:
            attachment = await asyncio.wait_for(
                self.attachment_lookup(record),
                timeout=min(120, self.settings.operation_timeout),
            )
        except asyncio.TimeoutError:
            raise MediaError(
                f"Timed out fetching saved Discord message {record['message_id']} for E{episode}."
            ) from None
        if not isinstance(attachment, dict):
            raise MediaError(
                "The main Discord account returned invalid attachment metadata."
            )
        if (
            str(attachment.get("message_id") or record["message_id"])
            != record["message_id"]
        ):
            raise MediaError(
                "Discord returned an attachment from a different saved message."
            )
        url = str(attachment.get("url") or "")
        if not url:
            raise MediaError(
                f"Saved Discord message {record['message_id']} has no video attachment URL."
            )
        filename = Path(str(attachment.get("filename") or "episode-video")).name
        extension = attachment_extension(filename, attachment.get("content_type"))
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem).strip("._-")[
            :80
        ]
        if not safe_name:
            safe_name = f"episode-{episode}"
        destination = work / f"{safe_name}{extension}"
        try:
            attachment_size = int(
                attachment.get("size") or record.get("size_bytes") or 0
            )
        except (TypeError, ValueError):
            attachment_size = 0
        session.download_stage = "Downloading"
        session.download_received = 0
        session.download_total = max(0, attachment_size)
        if foreground:
            session.playback_state = "Downloading from Discord"
            session.status_started_at = time.monotonic()
        self._event(
            f"DOWNLOAD_START id={session.id[:8]} E{episode} language={language} "
            f"message={record['message_id']} expected_bytes={attachment_size or 'unknown'}"
        )

        def progress(received: int, total: int):
            session.download_received = received
            if total > 0:
                session.download_total = total
            if foreground and session.current_episode is None:
                session.playback_state = "Downloading from Discord"
            if total > 0:
                percent = min(100, int(received * 100 / total))
                if percent >= session.download_last_log_percent + 10:
                    session.download_last_log_percent = percent
                    self._event(
                        f"DOWNLOAD_PROGRESS id={session.id[:8]} E{episode} "
                        f"{self._size_text(received)}/{self._size_text(total)} {percent}%"
                    )

        await download_discord_attachment(
            url,
            destination,
            max_bytes=self.settings.max_temp_bytes,
            timeout=self.settings.operation_timeout,
            expected_size=attachment_size or None,
            disk_root=self.disk_root,
            on_progress=progress,
        )
        self._event(
            f"FFPROBE_START id={session.id[:8]} E{episode} file={destination.name}"
        )
        playable, info = await ensure_playable(
            destination,
            max_bytes=self.settings.max_temp_bytes,
            timeout=self.settings.operation_timeout,
            disk_root=self.disk_root,
        )
        duration = float(info["format"]["duration"])
        if playable != destination:
            self._event(
                f"FFMPEG_REPAIRED id={session.id[:8]} E{episode} file={playable.name}",
                "yellow",
            )
        marker = {
            "version": 1,
            "mal_id": session.mal_id,
            "episode": episode,
            "language": language,
            "message_id": record["message_id"],
            "channel_id": record["channel_id"],
            "file": playable.name,
            "size_bytes": playable.stat().st_size,
            "duration_seconds": duration,
            "validated_at": time.time(),
        }
        marker_path = work / "ready.json"
        marker_path.write_text(
            json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        marker_path.chmod(0o600)
        if playable != destination:
            destination.unlink(missing_ok=True)
        session.download_episode = None
        session.download_stage = ""
        session.download_received = 0
        session.download_total = 0
        self._event(
            f"EPISODE_READY id={session.id[:8]} E{episode} language={language} "
            f"duration={self._clock(duration)} bytes={playable.stat().st_size} "
            f"message={record['message_id']}"
        )
        return PreparedEpisode(
            episode,
            work,
            playable,
            duration,
            language=language,
            message_id=record["message_id"],
        )
