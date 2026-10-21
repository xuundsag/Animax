"""Fetch community OP/ED intervals from AniSkip; absence is a no-skip result."""

from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger("animax.aniskip")
SKIP_TYPES = ("op", "ed")


def normalize_intervals(payload, duration: float) -> list[tuple[float, float]]:
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    closest: dict[str, tuple[float, dict]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("skipType") not in SKIP_TYPES:
            continue
        try:
            reported_length = float(row.get("episodeLength", duration))
            distance = abs(reported_length - duration)
        except (TypeError, ValueError, OverflowError):
            distance = float("inf")
        kind = row["skipType"]
        if kind not in closest or distance < closest[kind][0]:
            closest[kind] = (distance, row)

    intervals = []
    for _, row in closest.values():
        interval = row.get("interval") or {}
        try:
            start = max(0.0, float(interval["startTime"]))
            end = min(duration, float(interval["endTime"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if start < end <= duration and end - start < duration:
            intervals.append((start, end))
    intervals.sort()
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


async def get_skip_intervals(
    mal_id: int, episode: int, duration: float
) -> list[tuple[float, float]]:
    if mal_id < 1 or episode < 1 or duration <= 0:
        return []
    params = [("types[]", kind) for kind in SKIP_TYPES]
    params.append(("episodeLength", f"{duration:.3f}"))
    timeout = aiohttp.ClientTimeout(total=8, connect=4)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                f"https://api.aniskip.com/v2/skip-times/{mal_id}/{episode}",
                params=params,
                headers={"User-Agent": "ANIMAX/2.4.3"},
            ) as response:
                if response.status == 404:
                    return []
                response.raise_for_status()
                payload = await response.json()
        return normalize_intervals(payload, duration)
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, ValueError) as exc:
        log.info(
            "ANISKIP_UNAVAILABLE mal=%s episode=%s error=%s",
            mal_id,
            episode,
            type(exc).__name__,
        )
        return []
