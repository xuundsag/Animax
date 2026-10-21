"""Resolve anime titles and MAL IDs, preferring AniList with a Jikan fallback."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
from rapidfuzz import fuzz


class TitleError(RuntimeError):
    pass


def clean_title(value) -> str:
    if not isinstance(value, str):
        raise TitleError("Anime metadata did not include a valid title.")
    title = " ".join(value.split()).strip()
    if not title:
        raise TitleError("Anime metadata returned an empty title.")
    if title == "ONE PIECE":
        title = "One Piece"
    return title[:300]


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


def _candidate_titles(candidate: dict) -> list[str]:
    title = candidate.get("title")
    if isinstance(title, dict):
        values = [title.get(key) for key in ("english", "romaji", "native")]
    else:
        values = [title, candidate.get("title_english"), candidate.get("title_romaji")]
    return list(
        dict.fromkeys(
            " ".join(v.split()) for v in values if isinstance(v, str) and v.strip()
        )
    )


def select_best_anime(query: str, candidates: list[dict]):
    needle = " ".join(query.casefold().split())
    ranked = []
    for item in candidates:
        try:
            mal_id = int(item.get("idMal") or item.get("mal_id"))
        except (TypeError, ValueError):
            continue
        titles = _candidate_titles(item)
        if mal_id < 1 or not titles:
            continue
        score = max(
            max(
                fuzz.WRatio(needle, title.casefold()),
                fuzz.token_set_ratio(needle, title.casefold()),
            )
            for title in titles
        )
        title_data = item.get("title")
        english = (
            title_data.get("english")
            if isinstance(title_data, dict)
            else item.get("title_english")
        )
        preferred = next((t for t in titles if t == english), titles[0])
        ranked.append((score, mal_id, clean_title(preferred)))
    if not ranked:
        raise TitleError("No MAL-linked anime title matched that search.")
    ranked.sort(key=lambda result: result[0], reverse=True)
    score, mal_id, title = ranked[0]
    if score < 45:
        raise TitleError(
            "No close anime-title match was found. Try a MAL ID or a clearer title."
        )
    return mal_id, title, round(score)


async def _anilist_search(query: str):
    timeout = aiohttp.ClientTimeout(total=10, connect=4)
    payload = {
        "query": "query ($search: String) { Page(page: 1, perPage: 20) { media(search: $search, type: ANIME, isAdult: false) { idMal title { english romaji native } } } }",
        "variables": {"search": query},
    }
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        async with session.post("https://graphql.anilist.co", json=payload) as response:
            response.raise_for_status()
            data = await response.json()
    return data["data"]["Page"]["media"]


async def _jikan_search(query: str):
    timeout = aiohttp.ClientTimeout(total=10, connect=4)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        async with session.get(
            "https://api.jikan.moe/v4/anime", params={"q": query, "limit": 20}
        ) as response:
            response.raise_for_status()
            data = await response.json()
    return data.get("data", [])


async def resolve_anime(query: str, overrides_path: Path | None = None):
    query = " ".join(str(query).split()).strip()
    if not query:
        raise TitleError("Enter a MAL ID or anime title.")
    if query.isdecimal():
        mal_id = int(query)
        if mal_id < 1:
            raise TitleError("MAL ID must be positive.")
        override = title_override(mal_id, overrides_path)
        if override:
            return mal_id, override, 100
        try:
            title = await fetch_anime_title(mal_id, overrides_path)
        except TitleError:
            title = f"MAL {mal_id}"
        return mal_id, title, 100

    errors = []
    try:
        return select_best_anime(query, await _anilist_search(query))
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
        return select_best_anime(query, await _jikan_search(query))
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        KeyError,
        TypeError,
        ValueError,
        TitleError,
    ) as exc:
        errors.append(type(exc).__name__)
    raise TitleError("Anime search failed (" + ", ".join(errors) + "). Try the MAL ID.")


async def fetch_anime_title(mal_id: int, overrides_path: Path | None = None) -> str:
    if mal_id < 1:
        raise ValueError("MAL ID must be positive.")
    override = title_override(mal_id, overrides_path)
    if override:
        return override

    timeout = aiohttp.ClientTimeout(total=8, connect=4)
    errors = []
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
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
