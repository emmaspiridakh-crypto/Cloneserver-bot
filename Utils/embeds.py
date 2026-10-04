import datetime

import discord


def fmt_date(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def info_embed(title: str, lines: list, color=None) -> discord.Embed:
    return discord.Embed(
        title=title,
        description="\n".join(lines),
        color=color or discord.Color.blurple(),
    )
