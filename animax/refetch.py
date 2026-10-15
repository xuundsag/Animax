from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from datetime import datetime, timezone

from .metadata import TitleError, clean_title
from .routing import category_id, marker
from .storage import Library

log = logging.getLogger("animax.refetch")
VIDEO_NAME = re.compile(r"^MAL[-_](\d+)[-_]EP[-_](\d+)[-_](dub|sub)\.mp4$", re.I)
MAX_JOURNAL_BYTES = 20 * 1024**2


def identifier(value):
    text = str(value)
    if not text.isdecimal() or not 0 < int(text) < 2**64:
        raise ValueError("Invalid identifier in history.")
    return str(int(text))


def title_from_caption(content, mal):
    title = str(content or "").split("|", 1)[0].strip().strip("*")
    title = re.sub(r"\\([*_~`|\\])", r"\1", title)
    return clean_title(title) if title else f"MAL {mal}"


def safe_permalink(url, channel, message, guild=None):
    if guild is not None:
        guild = identifier(guild)
    pattern = rf"https://discord\.com/channels/(?:@me|[0-9]+)/{channel}/{message}"
    if isinstance(url, str) and re.fullmatch(pattern, url):
        return url
    if guild:
        return f"https://discord.com/channels/{guild}/{channel}/{message}"
    return None


def add_record(
    candidate,
    account,
    channel,
    mal,
    language,
    episode,
    message,
    title,
    *,
    actual=None,
    jump_url=None,
    guild=None,
    size=None,
    uploaded_at=None,
):
    channel, message = identifier(channel), identifier(message)
    mal, episode = int(identifier(mal)), int(identifier(episode))
    if language not in {"sub", "dub"} or actual not in {None, "sub", "dub"}:
        raise ValueError("Invalid language in history.")
    current_title = candidate.data["anime"].get(str(mal), {}).get("title", "")
    if current_title and not current_title.startswith("MAL "):
        title = current_title
    actual = actual or language
    candidate.record(
        mal,
        clean_title(title),
        language,
        episode,
        account,
        channel,
        "uploaded",
        save=False,
        message_id=message,
        actual_language=actual,
        requested_language=language,
        guild_id=identifier(guild) if guild else None,
        jump_url=safe_permalink(jump_url, channel, message, guild),
        size_bytes=size if isinstance(size, int) and size >= 0 else None,
        needs_review=False,
        recovered=True,
    )
    row = candidate.data["anime"][str(mal)]["episodes"][language][str(episode)][
        "deliveries"
    ][f"{account}:{channel}"]
    if uploaded_at:
        for entry in row["messages"]:
            if entry["message_id"] == message:
                entry["uploaded_at"] = str(uploaded_at)


def import_snapshot(candidate, payload, account):
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("anime"), dict)
    ):
        raise ValueError("Unsupported anime.json snapshot.")
    records = []
    for mal, anime in payload["anime"].items():
        if not isinstance(anime, dict) or not isinstance(anime.get("episodes"), dict):
            raise ValueError("Malformed anime record.")
        title = anime.get("title") or f"MAL {mal}"
        for language, episodes in anime["episodes"].items():
            if language not in {"dub", "sub"} or not isinstance(episodes, dict):
                raise ValueError("Malformed episode records.")
            for episode, data in episodes.items():
                if not isinstance(data, dict) or not isinstance(
                    data.get("deliveries"), dict
                ):
                    raise ValueError("Malformed delivery records.")
                for delivery in data["deliveries"].values():
                    if not isinstance(delivery, dict) or str(
                        delivery.get("account_id")
                    ) != str(account):
                        continue
                    channel = identifier(delivery.get("channel_id"))
                    messages = delivery.get("messages", [])
                    if not isinstance(messages, list):
                        raise ValueError("Malformed message records.")
                    for message in messages:
                        if not isinstance(message, dict):
                            raise ValueError("Malformed message record.")
                        mid = identifier(message.get("message_id"))
                        actual = (
                            message.get("actual_language")
                            or delivery.get("actual_language")
                            or language
                        )
                        if actual not in {"dub", "sub"}:
                            raise ValueError("Invalid language in snapshot.")
                        guild = message.get("guild_id") or delivery.get("guild_id")
                        if guild:
                            identifier(guild)
                        identifier(mal)
                        identifier(episode)
                        clean_title(title)
                        records.append(
                            (
                                channel,
                                mal,
                                language,
                                episode,
                                mid,
                                title,
                                {
                                    "actual": actual,
                                    "guild": guild,
                                    "jump_url": message.get("jump_url"),
                                    "size": message.get("size_bytes"),
                                    "uploaded_at": message.get("uploaded_at"),
                                },
                            )
                        )
    for channel, mal, language, episode, message, title, details in records:
        add_record(
            candidate,
            account,
            channel,
            mal,
            language,
            episode,
            message,
            title,
            **details,
        )
    return len(records)


def normalize(candidate):
    count = 0
    for anime in candidate.data["anime"].values():
        for language, episodes in anime["episodes"].items():
            for episode in episodes.values():
                for row in episode["deliveries"].values():
                    messages = sorted(
                        row.get("messages", []),
                        key=lambda entry: int(entry["message_id"]),
                    )
                    if not messages:
                        continue
                    row["messages"] = messages
                    last = messages[-1]
                    row.update(
                        message_id=last["message_id"],
                        message_ids=[entry["message_id"] for entry in messages],
                        actual_language=last.get("actual_language", language),
                        jump_url=last.get("jump_url"),
                        status="uploaded",
                        needs_review=False,
                        progress_messages=[],
                    )
                    row["fallback_reason"] = (
                        "Recovered SUB fallback"
                        if language == "dub" and row["actual_language"] == "sub"
                        else None
                    )
                    count += len(messages)
    candidate.data["jobs"] = {}
    candidate.data["legacy_imported"] = datetime.now(timezone.utc).isoformat()
    return count


async def collect_history(channel, account, candidate, progress=None):
    scanned = 0
    restored_snapshot = False
    async for message in channel.history(limit=None):
        scanned += 1
        if progress and scanned % 500 == 0:
            await progress(scanned)
        if str(getattr(getattr(message, "author", None), "id", "")) != str(account):
            continue
        for attachment in getattr(message, "attachments", []):
            name = attachment.filename
            if name.lower() == "anime.json" and not restored_snapshot:
                if not 0 < getattr(attachment, "size", 0) <= MAX_JOURNAL_BYTES:
                    continue
                try:
                    raw = await attachment.read()
                    if len(raw) > MAX_JOURNAL_BYTES:
                        continue
                    payload = json.loads(raw.decode("utf-8-sig"))
                    restored_snapshot = import_snapshot(candidate, payload, account) > 0
                except (ValueError, UnicodeError, KeyError, TypeError, TitleError):
                    log.warning(
                        "Ignoring invalid anime.json snapshot in message %s", message.id
                    )
                continue
            match = VIDEO_NAME.fullmatch(name)
            if not match:
                continue
            mal, episode, actual = match.groups()
            actual = actual.lower()
            content = getattr(message, "content", "") or ""
            requested = (
                "dub"
                if actual == "sub" and "dub unavailable" in content.lower()
                else actual
            )
            try:
                add_record(
                    candidate,
                    account,
                    channel.id,
                    mal,
                    requested,
                    episode,
                    message.id,
                    title_from_caption(content, mal),
                    actual=actual,
                    jump_url=getattr(message, "jump_url", None),
                    guild=getattr(getattr(message, "guild", None), "id", None),
                    size=getattr(attachment, "size", None),
                )
            except (ValueError, TitleError):
                log.warning(
                    "Ignoring malformed episode attachment in message %s", message.id
                )
    return scanned


async def scope_channels(anchor, account):
    guild = getattr(anchor, "guild", None)
    if guild is None:
        return [anchor], 0
    member = getattr(guild, "me", None) or await guild.fetch_member(int(account))
    if str(member.id) != str(account):
        raise PermissionError("Cannot establish account permissions")
    scope = category_id(anchor)
    channels = [
        c
        for c in await guild.fetch_channels()
        if callable(getattr(c, "history", None))
        and (scope is None or category_id(c) == scope)
    ]
    readable = []
    for channel in channels:
        permissions = channel.permissions_for(member)
        if permissions.view_channel and permissions.read_message_history:
            readable.append(channel)
    if not readable:
        raise PermissionError("No readable channels in the selected scope")
    return readable, len(channels) - len(readable)


def rebuild_routes(candidate, channels, account):
    by_id = {str(c.id): c for c in channels if getattr(c, "guild", None)}
    owners = {}
    for mal, anime in candidate.data["anime"].items():
        for episodes in anime["episodes"].values():
            for episode in episodes.values():
                for row in episode["deliveries"].values():
                    cid = str(row.get("channel_id"))
                    if (
                        cid in by_id
                        and str(row.get("account_id")) == str(account)
                        and row.get("messages")
                    ):
                        owners.setdefault(cid, set()).add(mal)
    grouped = {}
    for cid, mals in owners.items():
        if len(mals) != 1:
            continue
        mal = next(iter(mals))
        channel = by_id[cid]
        if marker(channel) not in {None, int(mal)}:
            continue
        key = f"{account}:{channel.guild.id}:{category_id(channel) or 0}:{mal}"
        grouped.setdefault(key, []).append((channel, mal))
    routes = candidate.data.setdefault("routes", {})
    for key, matches in grouped.items():
        if len(matches) == 1:
            channel, mal = matches[0]
            routes[key] = dict(
                account_id=str(account),
                guild_id=str(channel.guild.id),
                category_id=str(category_id(channel)) if category_id(channel) else None,
                mal=int(mal),
                title=candidate.data["anime"][mal]["title"],
                channel_id=str(channel.id),
                channel_name=channel.name,
                creation_pending=False,
            )
        else:
            routes.pop(key, None)


async def run_refetch(bot, ctx, channel_id):
    status = None
    committed = False
    try:
        channel = await bot.channel_for(channel_id)
        if not callable(getattr(channel, "history", None)):
            await ctx.send(
                "That destination does not provide readable message history."
            )
            return
        status = await ctx.send(
            f"Refetching scope of <#{channel_id}> | Scanning category or guild history"
        )
        candidate = Library(bot.library.path, data=copy.deepcopy(bot.library.data))

        async def progress(scanned):
            try:
                await status.edit(
                    content=f"Refetching scope of <#{channel_id}> | {scanned} messages scanned"
                )
            except Exception:
                log.warning("Refetch progress edit failed", exc_info=True)

        async def collect_scope():
            channels, skipped = await scope_channels(channel, bot.user.id)
            scanned = 0
            for item in channels:
                base = scanned

                async def combined(count):
                    await progress(base + count)

                scanned += await collect_history(item, bot.user.id, candidate, combined)
            return scanned, channels, skipped

        scanned, channels, skipped = await asyncio.wait_for(
            collect_scope(), bot.cfg.refetch_timeout
        )
        rebuild_routes(candidate, channels, bot.user.id)
        count = normalize(candidate)
        if not count:
            text = "No recoverable uploads or valid anime.json export found. History was not changed."
        else:
            candidate.save()
            bot.library.data = candidate.data
            committed = True
            text = f"Refetched {count} uploaded messages from {scanned} scanned messages across {len(channels)} channels ({skipped} inaccessible channels skipped). anime.json saved."
    except asyncio.CancelledError:
        text = "Refetch cancelled. History was not changed."
    except Exception:
        log.exception("Refetch failed before journal commit")
        text = "Refetch failed. Check channel access or the timeout; history was not changed. Details: logs/bot.log"
    try:
        if status:
            await status.edit(content=text)
        else:
            await ctx.send(text)
    except Exception:
        log.warning(
            "Could not report refetch completion; committed=%s",
            committed,
            exc_info=True,
        )


async def refetch_command(bot, ctx, destination: str = ""):
    if bot.busy():
        await ctx.send(
            "Wait for the current job or maintenance task to stop before refetching."
        )
        return
    if bot.library.path.exists():
        text = bot.library.path.read_text(encoding="utf-8")
        try:
            stored = json.loads(text) if text.strip() else {}
        except ValueError:
            await ctx.send(
                "anime.json is malformed, not empty. Back it up and repair it before refetching."
            )
            return
        if not isinstance(stored, dict) or (
            stored
            and (
                stored.get("version") != 1
                or not isinstance(stored.get("anime", {}), dict)
                or not isinstance(stored.get("jobs", {}), dict)
            )
        ):
            await ctx.send(
                "anime.json has an invalid structure. Back it up and repair it before refetching."
            )
            return
        if stored.get("anime"):
            await ctx.send(
                "anime.json is not empty. Refetch will not overwrite existing history."
            )
            return
    destination = destination or str(bot.cfg.channel_id or ctx.channel.id)
    match = re.fullmatch(r"(?:<#([0-9]+)>|([0-9]+))", destination.strip())
    try:
        channel_id = (
            int(identifier(next(value for value in match.groups() if value)))
            if match
            else None
        )
    except ValueError:
        channel_id = None
    if channel_id is None:
        await ctx.send("Use `.refetch #channel` or `.refetch CHANNEL_ID`.")
        return
    bot.reset_request = None
    bot.refetch_task = asyncio.create_task(run_refetch(bot, ctx, channel_id))
    await bot.refetch_task
