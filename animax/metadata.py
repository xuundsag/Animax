"""Look up a real anime title by MAL ID; no credentials needed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp


class TitleError(RuntimeError):
    pass


def clean_title(value) -> str:
    if not isinstance(value, str):
        raise TitleError("Anime metadata did not include a valid title.")
    title = " ".join(value.split()).strip()
    if not title:
        raise TitleError("Anime metadata returned an empty title.")
    # Keep the familiar spelling for the user's example, without title-casing acronyms.
    if title == "ONE PIECE":
        title = "One Piece"
    return title[:300]


def episode_caption(title: str, episode: int) -> str:
    from discord.utils import escape_markdown

    heading = escape_markdown(clean_title(title))
    return " | ".join((f"**{heading}**", f"`E{episode}`"))


def title_override(mal_id: int, path: Path | None):
    if path is None or not path.exists():
        return None
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TitleError("Cannot read title_overrides.json as valid JSON.") from exc
    if not isinstance(overrides, dict):
        raise TitleError("title_overrides.json must contain a JSON object.")
    return clean_title(overrides[str(mal_id)]) if str(mal_id) in overrides else None


def provisional_title(title, mal_id):
    return str(title).strip().casefold() in {f"mal {mal_id}", f"anime-{mal_id}", ""}


def saved_anime_title(data, mal_id):
    anime = data.get("anime", {}).get(str(mal_id), {})
    candidates = [anime.get("title") if isinstance(anime, dict) else None]
    for group in ("routes", "jobs"):
        records = [
            item
            for item in data.get(group, {}).values()
            if isinstance(item, dict) and str(item.get("mal")) == str(mal_id)
        ]
        records.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        candidates.extend(item.get("title") for item in records)
    for value in candidates:
        try:
            title = clean_title(value)
        except TitleError:
            continue
        if not provisional_title(title, mal_id):
            return title
    return None


async def fetch_anime_title(mal_id: int, overrides_path: Path | None = None) -> str:
    if mal_id < 1:
        raise ValueError("MAL ID must be positive.")
    override = title_override(mal_id, overrides_path)
    if override:
        return override

    timeout = aiohttp.ClientTimeout(total=8, connect=4)
    errors = []
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        # AniList maps MAL IDs directly. One request per job, never per episode.
        try:
            async with session.post(
                "https://graphql.anilist.co",
                json={
                    "query": "query ($id: Int) { Media(idMal: $id, type: ANIME) { title { romaji english } } }",
                    "variables": {"id": mal_id},
                },
            ) as response:
                response.raise_for_status()
                data = await response.json()
                title = data["data"]["Media"]["title"]
                return clean_title(title.get("english") or title.get("romaji"))
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            KeyError,
            AttributeError,
            TypeError,
            ValueError,
            TitleError,
        ) as exc:
            errors.append(type(exc).__name__)
        try:
            async with session.get(
                f"https://api.jikan.moe/v4/anime/{mal_id}"
            ) as response:
                response.raise_for_status()
                data = (await response.json())["data"]
                return clean_title(data.get("title_english") or data.get("title"))
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            KeyError,
            AttributeError,
            TypeError,
            ValueError,
            TitleError,
        ) as exc:
            errors.append(type(exc).__name__)
    raise TitleError(
        "Could not look up the anime name (" + ", ".join(errors) + "). "
        "Retry later or add the MAL ID and title to title_overrides.json."
    )
