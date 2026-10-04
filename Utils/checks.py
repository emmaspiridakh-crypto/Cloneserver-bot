import discord


async def owner_check(interaction: discord.Interaction) -> bool:
    """Bot owners (stored in the database) and the main owner."""
    return await interaction.client.is_allowed(interaction.user.id)


async def emma_check(interaction: discord.Interaction) -> bool:
    """Main owner only, and only inside the home server."""
    bot = interaction.client
    return interaction.user.id == bot.owner_id and interaction.guild_id == bot.home_guild_id
