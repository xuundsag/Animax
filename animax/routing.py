"""Anime destinations scoped to the log channel's guild and category."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import unicodedata

import discord

from .metadata import provisional_title

log = logging.getLogger("animax.routing")
CHANNEL_NAME_LIMIT = 64


def category_id(channel):
    return getattr(channel, "category_id", None) or getattr(
        getattr(channel, "parent", None), "category_id", None
    )


def legacy_slug(title, mal):
    text = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    )
    return (
        re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:90].rstrip("-") or f"anime-{mal}"
    )


def with_mal(name, mal):
    suffix = f"-{mal}"
    if name.endswith(suffix) and len(name) <= CHANNEL_NAME_LIMIT:
        return name
    budget = CHANNEL_NAME_LIMIT - len(suffix)
    return name[:budget].rstrip("-") + suffix


def slug(title, mal):
    if provisional_title(title, mal):
        return f"anime-{mal}"
    text = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    )
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if not text:
        return f"anime-{mal}"
    if len(text) <= CHANNEL_NAME_LIMIT:
        return text
    # Short, readable title fragment, with MAL identity retained to avoid collisions.
    words = text.split("-")[:6]
    budget = min(48, CHANNEL_NAME_LIMIT - len(str(mal)) - 1)
    while len(words) > 1 and len("-".join(words)) > budget:
        words.pop()
    return with_mal("-".join(words)[:budget].rstrip("-"), mal)


def marker(channel):
    match = re.search(r"\bANIMAX MAL:(\d+)\b", getattr(channel, "topic", "") or "")
    return int(match[1]) if match else None


def save_route(library, key, value):
    routes = library.data.setdefault("routes", {})
    previous = copy.deepcopy(routes.get(key))
    routes[key] = value
    try:
        library.save()
    except Exception:
        if previous is None:
            routes.pop(key, None)
        else:
            routes[key] = previous
        raise


class LogContext:
    def __init__(self, channel, origin=None):
        self.channel, self.origin = channel, origin

    async def send(self, content=None, **kwargs):
        if self.origin and self.origin.channel.id == self.channel.id:
            return await self.origin.send(content, **kwargs)
        return await self.channel.send(content, **kwargs)


class Router:
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

    async def resolve(self, anchor, job):
        guild = getattr(anchor, "guild", None)
        if guild is None:
            raise ValueError(
                "Automatic anime channels require a server log/command channel, not a DM."
            )
        async with self.lock:
            scope = category_id(anchor)
            key = f"{self.bot.user.id}:{guild.id}:{scope or 0}:{job.mal}"
            all_channels = await guild.fetch_channels()
            channels = [
                c
                for c in all_channels
                if isinstance(c, discord.TextChannel)
                or getattr(c, "type", None) == discord.ChannelType.text
            ]
            scoped = [c for c in channels if category_id(c) == scope]
            saved = self.bot.library.data.get("routes", {}).get(key, {})
            name = slug(job.title, job.mal)
            old_name = legacy_slug(job.title, job.mal)
            compatible_names = {
                name,
                with_mal(name, job.mal),
                old_name,
                f"{old_name}-{job.mal}",
            }
            target = next(
                (c for c in scoped if str(c.id) == str(saved.get("channel_id"))), None
            )
            if target and marker(target) not in {None, job.mal}:
                raise ValueError(
                    "Saved anime channel has a conflicting MAL marker; inspect anime.json and the channel topic."
                )
            if target is None:
                matches = [c for c in scoped if marker(c) == job.mal]
                if not matches:
                    matches = [
                        c
                        for c in scoped
                        if c.name in compatible_names and marker(c) in {None, job.mal}
                    ]
                if len(matches) > 1:
                    raise ValueError(
                        "Multiple anime channels match. Resolve the duplicate names/topics before resuming."
                    )
                target = matches[0] if matches else None
            route = dict(
                account_id=str(self.bot.user.id),
                guild_id=str(guild.id),
                category_id=str(scope) if scope else None,
                mal=job.mal,
                title=job.title,
                log_channel_id=str(anchor.id),
            )
            if target is None:
                if saved.get("creation_pending"):
                    raise ValueError(
                        "An earlier channel creation is unresolved. Inspect the server or manually create the matching anime channel before resuming; no blind duplicate creation."
                    )
                if any(c.name == name for c in scoped):
                    name = with_mal(name, job.mal)
                if any(c.name == name for c in scoped):
                    raise ValueError(
                        "Anime channel name conflicts with another MAL. Resolve its name/topic first."
                    )
                category = (
                    next((c for c in all_channels if c.id == scope), None)
                    if scope
                    else None
                )
                if scope and category is None:
                    raise ValueError(
                        "The log category disappeared; choose a valid log channel."
                    )
                save_route(
                    self.bot.library,
                    key,
                    dict(route, creation_pending=True, channel_name=name),
                )
                try:
                    target = await guild.create_text_channel(
                        name,
                        category=category,
                        topic=f"ANIMAX MAL:{job.mal}",
                        overwrites=anchor.overwrites,
                        reason=f"ANIMAX anime {job.mal}",
                    )
                except discord.HTTPException as exc:
                    if 400 <= exc.status < 500:
                        save_route(
                            self.bot.library, key, dict(route, creation_pending=False)
                        )
                    raise
            self.bot.check_permissions(target)
            if (
                target.name == f"anime-{job.mal}"
                and marker(target) == job.mal
                and (
                    saved.get("title") == f"MAL {job.mal}"
                    or saved.get("channel_provisional")
                )
                and name != target.name
                and not any(c.id != target.id and c.name == name for c in scoped)
            ):
                try:
                    renamed = await asyncio.wait_for(
                        target.edit(name=name, reason="ANIMAX title recovered"),
                        timeout=10,
                    )
                    if renamed is not None:
                        target = renamed
                except Exception:
                    log.warning(
                        "Could not rename placeholder channel %s; keeping its existing destination",
                        target.id,
                        exc_info=True,
                    )
            save_route(
                self.bot.library,
                key,
                dict(
                    route,
                    channel_id=str(target.id),
                    channel_name=target.name,
                    channel_provisional=target.name == f"anime-{job.mal}",
                    creation_pending=False,
                ),
            )
            return target
