from __future__ import annotations

import logging

import discord

from .refetch import refetch_command
from .reset import handle_reset
from .search import find_uploads
from .settings import settings_command

log = logging.getLogger("animax.commands")

COMMAND_HELP = {
    "download": "`.download MAL_ID dub|sub START END [resume|force]`: inclusive range, one whole MP4 each. Example: `.download 21 dub 1 10`. Force can duplicate confirmed uploads. Pending sends are still checked before any retry.",
    "status": "`.status`: show the current or last job in one line.",
    "cancel": "`.cancel`: stop the job; uploaded messages and saved history remain.",
    "resume": "`.resume` or `.retry`: resume the latest job and skip uploaded episodes. Pending uploads are checked against destination history before any retry.",
    "history": "`.history MAL_ID`: show local delivery totals for that anime.",
    "search": "`.search QUERY`: find up to five uploaded episodes. Try `.search One piece episode 1` or `.search eisode 1 onepimse`. Searches anime.json, not the source catalog.",
    "getlist": "`.getlist`: attach anime.json only. It contains account/channel/message metadata; share carefully.",
    "reset": "`.reset` asks for confirmation before clearing local anime history, saved jobs and video cache. `.reset cancel` aborts. Existing Discord messages, settings and cooldowns stay.",
    "refetch": "`.refetch #channel` or `.refetch CHANNEL_ID`: recover an empty/missing anime.json from your exports and ANIMAX uploads. Existing history is not overwritten.",
    "settings": "`.settings` lists fields. `.settings FIELD` shows valid values. `.settings quality 1080` validates and saves a change while idle.",
    "help": "`.help [name]`: list commands or show specific syntax.",
}


def register_commands(bot):
    @bot.command(name="download", ignore_extra=False)
    async def download_entry(
        ctx, mal_id: int, language: str, start: int, end: int, mode: str = "resume"
    ):
        await bot.download_command(ctx, mal_id, language, start, end, mode)

    @bot.command(name="status", ignore_extra=False)
    async def status_entry(ctx):
        await ctx.send(bot.job.episode_text() if bot.job else "Ready. No active job.")

    @bot.command(name="cancel", ignore_extra=False)
    async def cancel_entry(ctx):
        if bot.worker and not bot.worker.done():
            bot.worker.cancel()
            await ctx.send("Cancelling. Uploaded episodes are saved.")
        elif bot.refetch_task and not bot.refetch_task.done():
            bot.refetch_task.cancel()
            await ctx.send("Cancelling refetch. Existing history stays unchanged.")
        else:
            await ctx.send("No active job to cancel.")

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
            line = f"{title} `E{result['episode']}` : [Click here]({url})"
            if result["requested_language"] == "dub" and result["language"] == "sub":
                line += " · SUB fallback"
            lines.append(line)
        await ctx.send("\n".join(lines) if lines else "No matching uploaded episodes.")

    @bot.command(name="getlist", ignore_extra=False)
    async def getlist_entry(ctx):
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
    async def resume_entry(ctx):
        if bot.worker and not bot.worker.done():
            await ctx.send("A job is already active. Use .status or .cancel.")
            return
        record = bot.library.latest_job(bot.user.id)
        if record:
            await bot.resume_record(record, ctx)
        else:
            await ctx.send("No saved job for this account.")

    @bot.command(name="reset", ignore_extra=False)
    async def reset_entry(ctx, action: str = "", token: str = ""):
        await handle_reset(bot, ctx, action, token)

    @bot.command(name="settings", ignore_extra=False)
    async def settings_entry(ctx, field: str = "", *, value: str = ""):
        await settings_command(bot, ctx, field, value)

    @bot.command(name="refetch", ignore_extra=False)
    async def refetch_entry(ctx, destination: str):
        await refetch_command(bot, ctx, destination)
