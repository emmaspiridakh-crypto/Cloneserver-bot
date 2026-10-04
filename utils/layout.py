import datetime

import discord


def fmt_date(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def text_block(title: str, lines: list) -> str:
    body = "\n".join(lines)
    return f"## {title}\n{body}" if body else f"## {title}"


def info_view(title: str, lines: list, color=None, items=None) -> discord.ui.LayoutView:
    """A static Components V2 message: one container with a title and lines.
    `items` are extra components (for example an ActionRow) placed under a separator."""
    view = discord.ui.LayoutView(timeout=None)
    children = [discord.ui.TextDisplay(text_block(title, lines))]
    if items:
        children.append(discord.ui.Separator())
        children.extend(items)
    view.add_item(discord.ui.Container(*children, accent_colour=color or discord.Colour.blurple()))
    return view
