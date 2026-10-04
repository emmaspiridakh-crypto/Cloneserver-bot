import asyncio
import logging

import discord

from utils.cloner import (
    LABELS,
    OPTIONS,
    InteractionReporter,
    build_snapshot,
    run_copy,
)
from utils.layout import fmt_date, info_view, text_block

log = logging.getLogger("panels")

BLURPLE = discord.Colour.blurple()


class OwnedView(discord.ui.LayoutView):
    def __init__(self, user_id: int, timeout: float = 900):
        super().__init__(timeout=timeout)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("> This panel belongs to someone else.", ephemeral=True)
            return False
        return True


class ActionButton(discord.ui.Button):
    def __init__(self, label, style, handler):
        super().__init__(label=label, style=style)
        self.handler = handler

    async def callback(self, interaction: discord.Interaction):
        await self.handler(interaction)


class ToggleButton(discord.ui.Button):
    def __init__(self, panel: "CopyPanel", key: str):
        on = key in panel.selected
        super().__init__(
            label=f"{LABELS[key]} | {'ON' if on else 'OFF'}",
            style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
        )
        self.panel = panel
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        self.panel.selected.symmetric_difference_update({self.key})
        await self.panel.refresh(interaction)


class TargetModal(discord.ui.Modal, title="Target Server"):
    target_id = discord.ui.TextInput(
        label="Target server ID",
        placeholder="123456789012345678",
        min_length=15,
        max_length=22,
    )

    def __init__(self, panel: "CopyPanel"):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.target_id.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message("> That is not a valid server ID.", ephemeral=True)
            return
        await self.panel.start_copy(interaction, int(raw))


class CopyPanel(OwnedView):
    def __init__(self, bot, user_id: int, source_guild: discord.Guild | None = None, clone: dict | None = None):
        super().__init__(user_id)
        self.bot = bot
        self.source_guild = source_guild
        self.clone = clone
        self.selected: set = set()
        self.build()

    # ---- layout
    def text(self) -> str:
        if self.clone:
            head = [
                f"> Saved clone | {self.clone['guild_name']}",
                f"> Saved on | {fmt_date(self.clone['created_at'])}",
            ]
        else:
            head = [f"> Source | {self.source_guild.name}"]
        sel = ", ".join(LABELS[k] for k in OPTIONS if k in self.selected) or "nothing"
        body = [
            f"> Selected | {sel}",
            "",
            "- Roles and Channels already in the target server are deleted before copying.",
            "- The bot needs Administrator in the target server.",
        ]
        if not self.clone:
            body.append("- Make a Clone saves the whole server inside the bot, to load later.")
        return text_block("Copy Server", head + body)

    def build(self):
        self.clear_items()

        toggles = discord.ui.ActionRow(*[ToggleButton(self, k) for k in OPTIONS])
        all_row = discord.ui.ActionRow(
            ActionButton("Copy All", discord.ButtonStyle.primary, self.on_copy_all)
        )

        buttons = [
            ActionButton("Copy Server", discord.ButtonStyle.success, self.on_copy_server),
            ActionButton("Cancel", discord.ButtonStyle.danger, self.on_cancel),
        ]
        if self.clone:
            buttons.append(ActionButton("Back", discord.ButtonStyle.secondary, self.on_back))
            buttons.append(ActionButton("Delete Clone", discord.ButtonStyle.danger, self.on_delete))
        else:
            buttons.append(ActionButton("Make a Clone", discord.ButtonStyle.primary, self.on_make_clone))
            buttons.append(ActionButton("My Clones", discord.ButtonStyle.secondary, self.on_my_clones))

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(self.text()),
                discord.ui.Separator(),
                toggles,
                all_row,
                discord.ui.ActionRow(*buttons),
                accent_colour=BLURPLE,
            )
        )

    async def refresh(self, interaction: discord.Interaction):
        self.build()
        await interaction.response.edit_message(view=self)

    # ---- handlers
    async def on_copy_all(self, interaction: discord.Interaction):
        self.selected = set(OPTIONS)
        await self.refresh(interaction)

    async def on_copy_server(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TargetModal(self))

    async def on_cancel(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=info_view("Copy Server", ["> Cancelled."]))

    async def on_make_clone(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            snap = await build_snapshot(self.source_guild)
            await self.bot.db.save_clone(self.user_id, self.source_guild.id, self.source_guild.name, snap)
        except Exception:
            log.exception("Make a Clone failed")
            await interaction.followup.send("> Could not save the clone.", ephemeral=True)
            return
        await interaction.followup.send(f"> Clone saved | {self.source_guild.name}", ephemeral=True)

    async def on_my_clones(self, interaction: discord.Interaction):
        await show_clones(interaction, self.bot, self.user_id, self.source_guild)

    async def on_back(self, interaction: discord.Interaction):
        await show_clones(interaction, self.bot, self.user_id, self.source_guild)

    async def on_delete(self, interaction: discord.Interaction):
        await self.bot.db.delete_clone(self.user_id, self.clone["id"])
        await show_clones(interaction, self.bot, self.user_id, self.source_guild)

    # ---- the actual copy
    async def start_copy(self, interaction: discord.Interaction, target_id: int):
        bot = self.bot

        if not self.selected:
            await interaction.response.send_message("> Select at least one option first.", ephemeral=True)
            return
        if not self.clone and target_id == self.source_guild.id:
            await interaction.response.send_message("> The target cannot be the source server.", ephemeral=True)
            return
        if target_id == bot.home_guild_id and self.user_id != bot.owner_id:
            await interaction.response.send_message("> You cannot copy into this server.", ephemeral=True)
            return

        target = bot.get_guild(target_id)
        if target is not None:
            try:
                member = await target.fetch_member(self.user_id)
                allowed = member.guild_permissions.administrator
            except discord.HTTPException:
                allowed = False
            if not allowed:
                await interaction.response.send_message(
                    "> You need Administrator in the target server.", ephemeral=True
                )
                return

        await interaction.response.edit_message(view=info_view("Copy Server", ["> Preparing the copy"]))
        self.stop()

        try:
            snap = self.clone["snapshot"] if self.clone else await build_snapshot(self.source_guild)
        except Exception:
            log.exception("Snapshot failed")
            await interaction.edit_original_response(
                view=info_view("Copy failed", ["> Could not read the source server."], discord.Colour.red())
            )
            return

        selected = set(self.selected)

        if target is not None:
            asyncio.create_task(run_copy(snap, selected, target, InteractionReporter(interaction)))
            return

        # bot is not in the target server: invite with Administrator, copy starts on join
        await bot.db.set_pending(target_id, self.user_id, snap, sorted(selected))
        url = discord.utils.oauth_url(
            bot.user.id,
            permissions=discord.Permissions(administrator=True),
            guild=discord.Object(id=target_id),
            disable_guild_select=True,
        )
        link_row = discord.ui.ActionRow(discord.ui.Button(label="Add Bot", url=url))
        await interaction.edit_original_response(
            view=info_view(
                "Copy Server",
                [
                    "> The bot is not in the target server yet.",
                    "> Press Add Bot and accept the invite with Administrator.",
                    "",
                    "- The copy starts by itself when the bot joins.",
                    "- Progress is sent to your DMs.",
                    "- The bot leaves the server when it is finished.",
                    "- The link is valid for 60 minutes.",
                ],
                items=[link_row],
            )
        )


# ------------------------------------------------------------- clone list
class ClonesView(OwnedView):
    def __init__(self, bot, user_id: int, clones: list, source_guild: discord.Guild | None):
        super().__init__(user_id)
        self.bot = bot
        self.clones = clones
        self.source_guild = source_guild
        self.build()

    def build(self):
        self.clear_items()

        options = [
            discord.SelectOption(
                label=(c["guild_name"] or "Unnamed")[:100],
                value=str(c["id"]),
                description=f"Saved {fmt_date(c['created_at'])}",
            )
            for c in self.clones[:25]
        ]
        select = discord.ui.Select(placeholder="Select a clone", options=options)
        select.callback = self.on_select

        buttons = []
        if self.source_guild is not None:
            buttons.append(ActionButton("Back", discord.ButtonStyle.secondary, self.on_back))
        buttons.append(ActionButton("Cancel", discord.ButtonStyle.danger, self.on_cancel))

        lines = [f"> {c['guild_name']} | {fmt_date(c['created_at'])}" for c in self.clones]
        lines += ["", "- Select a clone to load it into a server."]

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text_block("My Clones", lines)),
                discord.ui.Separator(),
                discord.ui.ActionRow(select),
                discord.ui.ActionRow(*buttons),
                accent_colour=BLURPLE,
            )
        )

    async def on_select(self, interaction: discord.Interaction):
        clone_id = int(interaction.data["values"][0])
        clone = await self.bot.db.get_clone(self.user_id, clone_id)
        if clone is None:
            await interaction.response.send_message("> That clone no longer exists.", ephemeral=True)
            return
        panel = CopyPanel(self.bot, self.user_id, source_guild=self.source_guild, clone=clone)
        await interaction.response.edit_message(view=panel)

    async def on_back(self, interaction: discord.Interaction):
        panel = CopyPanel(self.bot, self.user_id, source_guild=self.source_guild)
        await interaction.response.edit_message(view=panel)

    async def on_cancel(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=info_view("My Clones", ["> Closed."]))


async def show_clones(interaction: discord.Interaction, bot, user_id: int, source_guild):
    clones = await bot.db.list_clones(user_id)
    if not clones:
        if source_guild is not None:
            panel = CopyPanel(bot, user_id, source_guild=source_guild)
            await interaction.response.edit_message(view=panel)
            await interaction.followup.send("> You have no saved clones.", ephemeral=True)
        else:
            await interaction.response.edit_message(view=info_view("My Clones", ["> You have no saved clones."]))
        return
    view = ClonesView(bot, user_id, clones, source_guild)
    await interaction.response.edit_message(view=view)

        
