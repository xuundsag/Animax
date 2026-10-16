"""Fuzzy lookup of uploaded messages in the local anime.json journal."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value)).casefold()
    return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


def parse_query(query):
    text = normalize(query)
    # Attached forms: E1, EP01, episode12 (and common misspellings).
    match = re.search(r"\b(?:episode|epsiode|eisode|episdoe|ep|e)\s*(\d+)\b", text)
    if match:
        return (text[: match.start()] + " " + text[match.end() :]).strip(), int(
            match.group(1)
        )
    tokens = text.split()
    for index, token in enumerate(tokens):
        if fuzz.ratio(token, "episode") >= 70:
            for other in (index + 1, index - 1):
                if 0 <= other < len(tokens) and tokens[other].isdigit():
                    episode = int(tokens[other])
                    return " ".join(
                        t for i, t in enumerate(tokens) if i not in {index, other}
                    ), episode
    numbers = [i for i, t in enumerate(tokens) if t.isdigit()]
    if len(numbers) == 1 and len(tokens) > 1:
        index = numbers[0]
        return " ".join(t for i, t in enumerate(tokens) if i != index), int(
            tokens[index]
        )
    return text, None


def uploaded_candidates(data, account_id):
    for mal, anime in data.get("anime", {}).items():
        for requested, episodes in anime.get("episodes", {}).items():
            for number, episode in episodes.items():
                if not str(number).isdigit():
                    continue
                for delivery in episode.get("deliveries", {}).values():
                    if str(delivery.get("account_id")) != str(account_id):
                        continue
                    messages = delivery.get("messages") or []
                    if not messages:
                        continue
                    message = messages[-1]
                    yield {
                        "mal_id": int(mal),
                        "title": anime.get("title", f"MAL {mal}"),
                        "episode": int(number),
                        "language": message.get("actual_language")
                        or delivery.get("actual_language")
                        or requested,
                        "requested_language": requested,
                        "channel_id": str(delivery["channel_id"]),
                        "guild_id": message.get("guild_id") or delivery.get("guild_id"),
                        "message_id": str(message["message_id"]),
                        "jump_url": message.get("jump_url"),
                        "uploaded_at": message.get("uploaded_at", ""),
                    }


def find_uploads(data, query, account_id, limit=5):
    title, episode = parse_query(query)
    compact = title.replace(" ", "")
    ranked = []
    for result in uploaded_candidates(data, account_id):
        if episode is not None and result["episode"] != episode:
            continue
        normalized = normalize(result["title"])
        score = (
            100
            if not title or title == str(result["mal_id"])
            else max(
                fuzz.WRatio(title, normalized),
                fuzz.WRatio(compact, normalized.replace(" ", "")),
            )
        )
        if score < 60:
            continue
        ranked.append((score, result["uploaded_at"], result))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    results = []
    seen = set()
    for score, _, item in ranked:
        key = (item["mal_id"], item["episode"], item["channel_id"], item["language"])
        if key in seen:
            continue
        seen.add(key)
        results.append(dict(item, score=round(score, 1)))
        if len(results) >= limit:
            break
    return results
