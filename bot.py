import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from keep_alive import keep_alive
from utils.db import Database

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("clonebot")

TOKEN = os.environ["TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
HOME_GUILD_ID = int(os.environ["HOME_GUILD_ID"])
TURSO_URL = os.environ["TURSO_DATABASE_URL"]
TURSO_TOKEN = os.environ["TURSO_AUTH_TOKEN"]

EXTENSIONS = ("cogs.copy", "cogs.owners", "cogs.events")


class CloneBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
        )
        self.db = Database(TURSO_URL, TURSO_TOKEN)
        self.owner_id = OWNER_ID
        self.home_guild_id = HOME_GUILD_ID

    async def is_allowed(self, user_id: int) -> bool:
        return user_id == self.owner_id or await self.db.is_owner(user_id)

    async def setup_hook(self):
        await self.db.connect()
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        await self.tree.sync()
        await self.tree.sync(guild=discord.Object(id=HOME_GUILD_ID))

    async def close(self):
        await self.db.close()
        await super().close()


bot = CloneBot()


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "> You are not allowed to use this command."
    else:
        log.exception("Command error", exc_info=error)
        msg = "> Something went wrong."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
