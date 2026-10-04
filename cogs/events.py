import asyncio
import logging

import discord
from discord.ext import commands

from utils.cloner import DMReporter, run_copy

log = logging.getLogger("events")


class EventsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        log.info("Logged in as %s", self.bot.user)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        job = await self.bot.db.pop_pending(guild.id)
        if job is None:
            return

        try:
            user = await self.bot.fetch_user(job["user_id"])
        except discord.HTTPException:
            await guild.leave()
            return
        reporter = DMReporter(user)

        await asyncio.sleep(3)

        try:
            member = await guild.fetch_member(job["user_id"])
            allowed = member.guild_permissions.administrator
        except discord.HTTPException:
            allowed = False

        if not allowed:
            try:
                await user.send("> Copy cancelled | you need Administrator in the target server.")
            except discord.HTTPException:
                pass
            await guild.leave()
            return

        try:
            await run_copy(job["snapshot"], set(job["options"]), guild, reporter)
        except Exception:
            log.exception("Copy into %s failed", guild.id)
        finally:
            await asyncio.sleep(2)
            try:
                await guild.leave()
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(EventsCog(bot))
