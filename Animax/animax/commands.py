from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .refetch import refetch_command
from .reset import handle_reset
from .search import find_uploads
from .settings import settings_command

log = logging.getLogger("animax.commands")

COMMAND_HELP = {
    "download": "`.download MAL_ID dub|sub START END [resume|force] [#channel]`: inclusive range, one whole MP4 each. First use in a server requires the destination channel mention last, e.g. `.download 21 dub 1 10 #one-piece`. Later downloads reuse anime.json and ignore a supplied channel. Force can duplicate confirmed uploads; pending sends are still checked before retry.",
    "status": "`.status`: show the serialized download queue and any active Go Live stream.",
    "cancel": "`.cancel [JOB_ID|all]`: stop selected download jobs or queued jobs; history and cached videos remain. Stream controls use the `?` prefix on the separate streaming account.",
    "resume": "`.resume [JOB_ID|all]` or `.retry`: queue interrupted/failed jobs; bare resume selects the latest inactive unfinished job. Confirmed episodes are skipped; unresolved uploads are checked. Old jobs keep their saved upload destination.",
    "history": "`.history MAL_ID`: show local delivery totals for that anime.",
    "search": "`.search QUERY`: find up to five uploaded episodes. Try `.search One piece episode 1` or `.search eisode 1 onepimse`. Searches anime.json, not the source catalog.",
    "getlist": "`.getlist`: attach anime.json only. It contains account/channel/message metadata; share carefully.",
    "reset": "`.reset` asks for confirmation before clearing local anime history, saved jobs and video cache. `.reset cancel` aborts. Existing Discord messages, settings and cooldowns stay.",
    "refetch": "`.refetch [#channel|CHANNEL_ID]`: rebuild missing/empty anime.json from readable text-history channels in that channel's category, or the entire guild if uncategorized. Defaults to the log channel, then this channel. One transaction; any scan failure leaves history unchanged.",
    "settings": "`.settings` lists fields. `.settings FIELD` shows valid values. `.settings quality 1080` validates and saves a change while idle.",
    "help": "`.help [name]`: list commands or show specific syntax.",
}


def parse_download_tail(tail):
    tokens = str(tail or "").split()
    mode = "resume"
    channel_mention = ""
    if not tokens:
        return mode, channel_mention
    if tokens[-1].lower() in {"resume", "force"}:
        if len(tokens) != 1:
            raise commands.BadArgument(
                "Put the destination channel mention at the very end of the command."
            )
        return tokens[0].lower(), channel_mention
    if len(tokens) > 2:
        raise commands.BadArgument(
            "Use an optional mode, then one channel mention as the final argument."
        )
    if len(tokens) == 2:
        if tokens[0].lower() not in {"resume", "force"}:
            raise commands.BadArgument(
                "Use `[resume|force]` before the final channel mention."
            )
        mode = tokens[0].lower()
    channel_mention = tokens[-1]
    return mode, channel_mention


def register_commands(bot):
    @bot.command(name="download", ignore_extra=False)
    async def download_entry(
        ctx, mal_id: int, language: str, start: int, end: int, *, tail: str = ""
    ):
        mode, channel_mention = parse_download_tail(tail)
        await bot.download_command(
            ctx, mal_id, language, start, end, mode, channel_mention
        )

    @bot.command(name="status", ignore_extra=False)
    async def status_entry(ctx):
        lines = [bot.scheduler.status_text()]
        if bot.stream_starting:
            lines.append("The separate `?` stream command listener is starting.")
        if bot.stream_manager:
            stream_status = bot.stream_manager.status(
                ctx.guild.id if getattr(ctx, "guild", None) else None
            )
            if stream_status.get("phase") or "No active" not in stream_status.get(
                "message", ""
            ):
                lines.append(stream_status["message"])
        await ctx.send("\n".join(lines)[:1900])

    @bot.command(name="cancel", ignore_extra=False)
    async def cancel_entry(ctx, selector: str = ""):
        if bot.refetch_task and not bot.refetch_task.done():
            bot.refetch_task.cancel()
            await ctx.send("Cancelling refetch. Existing history stays unchanged.")
        else:
            await ctx.send(await bot.scheduler.cancel(selector))

    @bot.command(name="help", aliases=["mphelp"], ignore_extra=False)
    async def help_entry(ctx, name: str = ""):
        if name:
            command = bot.get_command(name.lstrip(".").lower())
            await ctx.send(
                COMMAND_HELP[command.name] if command else "Unknown command. Use .help."
            )
            return
        names = ", ".join(COMMAND_HELP)
        await ctx.send(f"**Commands**\n```\n{names}\n```")

    @bot.command(name="search", ignore_extra=False)
    async def search_entry(ctx, *, query: str):
        lines = []
        for result in find_uploads(bot.library.data, query, bot.user.id):
            url = result.get("jump_url")
            if not url:
                try:
                    channel = await bot.channel_for(result["channel_id"])
                    guild = getattr(channel, "guild", None)
                    url = f"https://discord.com/channels/{guild.id if guild else '@me'}/{result['channel_id']}/{result['message_id']}"
                except Exception:
                    log.warning("Cannot resolve legacy search permalink", exc_info=True)
                    continue
            title = discord.utils.escape_markdown(result["title"])
            lines.append(f"{title} `E{result['episode']}` : [Click here]({url})")
        await ctx.send("\n".join(lines) if lines else "No matching uploaded episodes.")

    @bot.command(name="getlist", ignore_extra=False)
    async def getlist_entry(ctx):
        async with bot.scheduler.resources.lease("upload", None):
            # Atomic journal replacements leave this open file snapshot valid.
            attachment = discord.File(str(bot.library.path), filename="anime.json")
            try:
                await ctx.send(file=attachment)
            finally:
                attachment.close()

    @bot.command(name="history", ignore_extra=False)
    async def history_entry(ctx, mal_id: int):
        await ctx.send(bot.library.summary(mal_id).replace("\n", " | "))

    @bot.command(name="resume", aliases=["retry"], ignore_extra=False)
    async def resume_entry(ctx, selector: str = ""):
        records = [
            dict(record, id=key)
            for key, record in bot.library.data["jobs"].items()
            if str(record.get("account_id")) == str(bot.user.id)
            and record.get("status") != "finished"
            and not bot.scheduler.contains(key)
        ]
        if selector.lower() != "all":
            records = (
                [r for r in records if r["id"] == selector]
                if selector
                else sorted(records, key=lambda r: r.get("updated_at", ""))[-1:]
            )
        if not records:
            await ctx.send(
                "No matching resumable job for this account. Use .status for active/queued jobs."
            )
        for record in records:
            await bot.resume_record(record, ctx)

    @bot.command(name="reset", ignore_extra=False)
    async def reset_entry(ctx, action: str = "", token: str = ""):
        await handle_reset(bot, ctx, action, token)

    @bot.command(name="settings", ignore_extra=False)
    async def settings_entry(ctx, field: str = "", *, value: str = ""):
        await settings_command(bot, ctx, field, value)

    @bot.command(name="refetch", ignore_extra=False)
    async def refetch_entry(ctx, destination: str = ""):
        await refetch_command(bot, ctx, destination)
