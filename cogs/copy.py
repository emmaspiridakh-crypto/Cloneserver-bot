import discord
from discord import app_commands
from discord.ext import commands

from ui.panels import CopyPanel
from utils.checks import owner_check


class CopyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    copy = app_commands.Group(name="copy", description="Server copy tools", guild_only=True)

    @copy.command(name="server", description="Open the server copy panel")
    @app_commands.check(owner_check)
    async def server(self, interaction: discord.Interaction):
        panel = CopyPanel(self.bot, interaction.user.id, source_guild=interaction.guild)
        await interaction.response.send_message(view=panel, ephemeral=True)


async def setup(bot):
    await bot.add_cog(CopyCog(bot))
