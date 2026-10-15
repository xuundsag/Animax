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


async def fetch_anime_title(mal_id: int, overrides_path: Path | None = None) -> str:
    if mal_id < 1:
        raise ValueError("MAL ID must be positive.")
    if overrides_path and overrides_path.exists():
        overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        if not isinstance(overrides, dict):
            raise TitleError("title_overrides.json must contain a JSON object.")
        if str(mal_id) in overrides:
            return clean_title(overrides[str(mal_id)])

    timeout = aiohttp.ClientTimeout(total=15)
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
            TypeError,
            ValueError,
            TitleError,
        ) as exc:
            errors.append(type(exc).__name__)
    raise TitleError(
        "Could not look up the anime name (" + ", ".join(errors) + "). "
        "Retry later or add the MAL ID and title to title_overrides.json."
    )
