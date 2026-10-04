import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import emma_check


class OwnersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    make = app_commands.Group(name="make", description="Bot owner management", guild_only=True)
    remove = app_commands.Group(name="remove", description="Bot owner management", guild_only=True)

    @make.command(name="owner", description="Give a user access to the bot")
    @app_commands.describe(user="The user to add as a bot owner")
    @app_commands.check(emma_check)
    async def make_owner(self, interaction: discord.Interaction, user: discord.User):
        if user.bot:
            await interaction.response.send_message("> Bots cannot be owners.", ephemeral=True)
            return
        added = await self.bot.db.add_owner(user.id)
        msg = f"> Owner added | {user}" if added else f"> Already an owner | {user}"
        await interaction.response.send_message(msg, ephemeral=True)

    @remove.command(name="owner", description="Remove a bot owner and delete their clones")
    @app_commands.describe(user="The owner to remove")
    @app_commands.check(emma_check)
    async def remove_owner(self, interaction: discord.Interaction, user: discord.User):
        existed, deleted = await self.bot.db.remove_owner(user.id)
        if not existed:
            await interaction.response.send_message(f"> Not an owner | {user}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"> Owner removed | {user}\n> Clones deleted | {deleted}", ephemeral=True
        )


async def setup(bot):
    # registered only in the home server
    await bot.add_cog(OwnersCog(bot), guild=discord.Object(id=bot.home_guild_id))
