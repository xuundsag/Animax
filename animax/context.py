from __future__ import annotations

import asyncio

import discord
from discord.ext import commands


class CommandContext(commands.Context):
    async def cleanup_trigger(self):
        if not hasattr(self, "_cleanup_lock"):
            self._cleanup_lock = asyncio.Lock()
        async with self._cleanup_lock:
            if not hasattr(self, "_trigger_deleted"):
                self._trigger_deleted = False
                if self.bot.cfg.delete_trigger:
                    self._trigger_deleted = await self.bot.safe_delete(
                        self.message, "command trigger"
                    )
            return self._trigger_deleted

    async def send(self, content=None, **kwargs):
        deleted = await self.cleanup_trigger()
        if not deleted and "reference" not in kwargs:
            kwargs["reference"] = discord.MessageReference(
                message_id=self.message.id,
                channel_id=self.channel.id,
                guild_id=getattr(self.guild, "id", None),
                fail_if_not_exists=False,
            )
            kwargs["mention_author"] = False
        return await super().send(content, **kwargs)
