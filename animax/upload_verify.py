"""Bounded, read-only checks of messages in the destination channel."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import discord

CHECK_DELAYS = (0, 5, 15)
HISTORY_LIMIT = 500


@dataclass
class Verification:
    state: str
    message: object = None
    reason: str = ""


def classify_message(message, intent):
    if (
        str(getattr(getattr(message, "author", None), "id", "")) != intent["account_id"]
        or str(getattr(getattr(message, "channel", None), "id", ""))
        != intent["channel_id"]
    ):
        return "unrelated"
    nonce_matches = intent.get("nonce") is not None and str(
        getattr(message, "nonce", None)
    ) == str(intent["nonce"])
    attachments = getattr(message, "attachments", [])
    named = [
        a for a in attachments if getattr(a, "filename", None) == intent["filename"]
    ]
    if not named:
        return "conflict" if nonce_matches else "unrelated"
    if (
        len(attachments) != 1
        or getattr(named[0], "size", None) != intent["file_size_bytes"]
    ):
        return "conflict"
    if nonce_matches or getattr(message, "content", None) == intent["caption"]:
        return "found"
    return "conflict"


async def require_history_access(target, author_id):
    guild = getattr(target, "guild", None)
    if guild is None:
        return
    member = getattr(guild, "me", None)
    if member is None:
        member = await guild.fetch_member(int(author_id))
    if str(getattr(member, "id", "")) != str(author_id):
        raise PermissionError("Cannot establish this account's channel permissions.")
    permissions = target.permissions_for(member)
    if not permissions.view_channel or not permissions.read_message_history:
        # Discord can return an empty history rather than an error without this permission.
        raise PermissionError(
            "View Channel and Read Message History are required for safe verification."
        )


async def channel_anchor(target, timeout, author_id):
    async def newest():
        await require_history_access(target, author_id)
        async for message in target.history(limit=1, oldest_first=False):
            return int(message.id)
        return 0

    return await asyncio.wait_for(newest(), timeout=min(15, timeout))


async def _history_check(target, intent):
    anchor = intent.get("anchor_id")
    if anchor is not None:
        after = discord.Object(id=int(anchor)) if int(anchor) else None
    else:
        # Legacy reports have a timestamp but no pre-send snowflake anchor.
        after = datetime.fromisoformat(intent["started_utc"]) - timedelta(seconds=10)
    found, conflicts, count = [], 0, 0
    async for message in target.history(
        limit=HISTORY_LIMIT + 1, after=after, oldest_first=False
    ):
        count += 1
        if count > HISTORY_LIMIT:
            return Verification(
                "unknown",
                reason="History scan limit reached; absence is not established.",
            )
        kind = classify_message(message, intent)
        if kind == "found":
            found.append(message)
        elif kind == "conflict":
            conflicts += 1
    if conflicts or len(found) > 1:
        return Verification(
            "unknown", reason="Conflicting or multiple episode messages need review."
        )
    if found:
        return Verification("found", found[0], "Matching destination attachment found.")
    return Verification(
        "absent", reason="No matching message in the readable post-anchor history."
    )


async def verify_upload(target, intent, *, timeout, notify=lambda text: None):
    deadline = time.monotonic() + timeout

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise asyncio.TimeoutError()
        return value

    try:
        for index, delay in enumerate(CHECK_DELAYS):
            if delay:
                if remaining() <= delay:
                    return Verification(
                        "unknown", reason="Not enough time for delayed confirmation."
                    )
                await asyncio.sleep(delay)
            await asyncio.wait_for(
                require_history_access(target, intent["account_id"]),
                timeout=min(15, remaining()),
            )
            notify(f"Checking destination ({index + 1}/{len(CHECK_DELAYS)})")
            candidate = intent.get("message_id")
            if candidate:
                try:
                    message = await asyncio.wait_for(
                        target.fetch_message(int(candidate)),
                        timeout=min(15, remaining()),
                    )
                except discord.NotFound:
                    pass
                else:
                    if str(message.id) != str(candidate):
                        return Verification(
                            "unknown",
                            reason="Fetched message ID did not match; no resend.",
                        )
                    kind = classify_message(message, intent)
                    if kind == "found":
                        return Verification(
                            "found", message, "Message ID and attachment verified."
                        )
                    if kind != "unrelated":
                        return Verification(
                            "unknown",
                            reason="Confirmed message has unexpected attachment metadata.",
                        )
            checked = await asyncio.wait_for(
                _history_check(target, intent), timeout=min(15, remaining())
            )
            if checked.state != "absent":
                return checked
        if intent.get("message_id"):
            return Verification(
                "unknown",
                reason="A send returned a message ID, but it is not readable now; no resend.",
            )
        return checked
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return Verification(
            "unknown",
            reason=f"Destination check failed ({type(exc).__name__}); no resend.",
        )
