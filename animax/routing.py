"""Persist and resolve user-selected anime upload channels."""

from __future__ import annotations

import asyncio
import copy
import re

import discord


def category_id(channel):
    return getattr(channel, "category_id", None) or getattr(
        getattr(channel, "parent", None), "category_id", None
    )


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
    """Resolve a manually selected first-use channel and persist every destination."""

    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

    def _route_candidates(self, guild_id, mal):
        account_id = str(self.bot.user.id)
        candidates = []
        for key, route in self.bot.library.data.get("routes", {}).items():
            if not isinstance(route, dict):
                continue
            if (
                str(route.get("account_id")) == account_id
                and str(route.get("guild_id")) == str(guild_id)
                and str(route.get("mal")) == str(mal)
            ):
                candidates.append((key, route))
        return candidates

    def _history_channel_ids(self, guild_id, mal):
        account_id = str(self.bot.user.id)
        anime = self.bot.library.data.get("anime", {}).get(str(mal), {})
        ids, unknown = set(), set()
        for episodes in anime.get("episodes", {}).values():
            for episode in episodes.values():
                for key, row in episode.get("deliveries", {}).items():
                    if not isinstance(row, dict) or not row.get("messages"):
                        continue
                    row_account = str(row.get("account_id", str(key).split(":", 1)[0]))
                    if row_account != account_id:
                        continue
                    channel_id = str(row.get("channel_id", str(key).split(":", 1)[-1]))
                    row_guild = row.get("guild_id") or next(
                        (
                            m.get("guild_id")
                            for m in row.get("messages", [])
                            if m.get("guild_id")
                        ),
                        None,
                    )
                    if row_guild is None:
                        unknown.add(channel_id)
                    elif str(row_guild) == str(guild_id):
                        ids.add(channel_id)
        return ids, unknown

    @staticmethod
    def _validate_text_channel(target, guild_id):
        if (
            target is None
            or not getattr(target, "guild", None)
            or str(target.guild.id) != str(guild_id)
            or isinstance(target, discord.Thread)
            or getattr(target, "type", None) not in {None, discord.ChannelType.text}
        ):
            raise ValueError("Choose an accessible server text channel in this server.")
        return target

    def _choose_record(self, candidates, preferred_category):
        if not candidates:
            return None
        if preferred_category is not None:
            preferred = [
                item
                for item in candidates
                if str(item[1].get("category_id")) == str(preferred_category)
            ]
            if preferred:
                candidates = preferred
        channels = {}
        for key, route in candidates:
            channel_id = str(route.get("channel_id") or "")
            if channel_id:
                channels.setdefault(channel_id, (key, route))
            elif route.get("creation_pending"):
                raise ValueError(
                    "anime.json has an unresolved old channel-creation entry. Inspect it before continuing; no channel will be created automatically."
                )
        if len(channels) > 1:
            raise ValueError(
                "anime.json has multiple saved upload channels for this anime in this server. Resolve the route records before downloading."
            )
        return next(iter(channels.values()), None)

    async def saved_target(self, guild_id, mal, *, preferred_category=None):
        """Return a saved channel; a user-supplied channel is deliberately not involved."""
        async with self.lock:
            candidates = self._route_candidates(guild_id, mal)
            chosen = self._choose_record(candidates, preferred_category)
            if chosen:
                _, record = chosen
                channel_id = record.get("channel_id")
                if not channel_id:
                    raise ValueError(
                        f"anime.json has no saved channel ID for MAL {mal}."
                    )
                target = await self.bot.channel_for(channel_id)
                return self._validate_text_channel(target, guild_id)

            known_ids, unknown_ids = self._history_channel_ids(guild_id, mal)
            for channel_id in unknown_ids:
                target = await self.bot.channel_for(channel_id)
                if getattr(getattr(target, "guild", None), "id", None) == int(guild_id):
                    known_ids.add(str(channel_id))
            if len(known_ids) > 1:
                raise ValueError(
                    "anime.json history contains multiple upload channels for this anime in this server; resolve them before downloading."
                )
            if not known_ids:
                return None
            target = await self.bot.channel_for(next(iter(known_ids)))
            return self._validate_text_channel(target, guild_id)

    async def remember_target(self, target, job, anchor=None):
        guild = getattr(target, "guild", None)
        if guild is None:
            raise ValueError("Anime uploads require a server text channel.")
        self._validate_text_channel(target, guild.id)
        channel_marker = marker(target)
        if channel_marker not in {None, job.mal}:
            raise ValueError(
                "The selected channel topic is marked for another MAL ID. Choose the correct anime channel."
            )
        scope = category_id(target)
        key = f"{self.bot.user.id}:{guild.id}:{scope or 0}:{job.mal}"
        async with self.lock:
            existing = self.bot.library.data.get("routes", {}).get(key, {})
            if existing.get("channel_id") and str(existing["channel_id"]) != str(
                target.id
            ):
                raise ValueError(
                    "anime.json already maps this anime/category to a different channel; resolve that route first."
                )
            route = dict(
                account_id=str(self.bot.user.id),
                guild_id=str(guild.id),
                category_id=str(scope) if scope else None,
                mal=job.mal,
                title=job.title,
                channel_id=str(target.id),
                channel_name=getattr(target, "name", ""),
                log_channel_id=str(anchor.id) if anchor is not None else None,
                creation_pending=False,
                route_version=3,
            )
            save_route(self.bot.library, key, route)
        return target

    async def resolve(self, anchor, job):
        guild = getattr(anchor, "guild", None)
        if guild is None:
            raise ValueError(
                "No server is available to resolve this anime's saved channel."
            )
        target = await self.saved_target(
            guild.id, job.mal, preferred_category=category_id(anchor)
        )
        if target is None:
            raise ValueError(
                f"No saved channel for MAL {job.mal}. Start it with `.download {job.mal} {job.language} {job.start} {job.end} #channel` first."
            )
        self.bot.check_permissions(target)
        return await self.remember_target(target, job, anchor)
