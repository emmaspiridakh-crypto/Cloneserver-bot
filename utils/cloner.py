import asyncio
import base64
import logging

import discord

log = logging.getLogger("cloner")

REASON = "Server clone"
OPTIONS = ("roles", "channels", "settings", "icon", "emojis")
LABELS = {
    "roles": "Roles",
    "channels": "Channels",
    "settings": "Settings",
    "icon": "Icon",
    "emojis": "Emoji",
}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.b64decode(data)


def _overwrites(channel) -> list:
    out = []
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Role):
            allow, deny = ow.pair()
            out.append({"role_id": target.id, "allow": allow.value, "deny": deny.value})
    return out


# ---------------------------------------------------------------- snapshot
async def build_snapshot(guild: discord.Guild) -> dict:
    flags = guild.system_channel_flags
    snap = {
        "version": 1,
        "source": {"id": guild.id, "name": guild.name},
        "settings": {
            "name": guild.name,
            "verification_level": guild.verification_level.value,
            "default_notifications": guild.default_notifications.value,
            "explicit_content_filter": guild.explicit_content_filter.value,
            "afk_timeout": guild.afk_timeout,
            "preferred_locale": guild.preferred_locale.value,
            "afk_channel_id": guild.afk_channel.id if guild.afk_channel else None,
            "system_channel_id": guild.system_channel.id if guild.system_channel else None,
            "flags": {
                "join_notifications": flags.join_notifications,
                "premium_subscriptions": flags.premium_subscriptions,
                "guild_reminder_notifications": flags.guild_reminder_notifications,
                "join_notification_replies": flags.join_notification_replies,
            },
        },
        "icon": None,
        "roles": [],
        "categories": [],
        "channels": [],
        "emojis": [],
    }

    if guild.icon:
        try:
            snap["icon"] = _b64(await guild.icon.read())
        except discord.HTTPException:
            pass

    # roles: highest first, managed (bot) roles skipped
    for r in sorted(guild.roles, key=lambda x: x.position, reverse=True):
        if r.managed:
            continue
        snap["roles"].append(
            {
                "id": r.id,
                "name": r.name,
                "color": r.colour.value,
                "permissions": r.permissions.value,
                "hoist": r.hoist,
                "mentionable": r.mentionable,
                "default": r.is_default(),
            }
        )

    for c in sorted(guild.categories, key=lambda x: x.position):
        snap["categories"].append({"id": c.id, "name": c.name, "overwrites": _overwrites(c)})

    for c in sorted(guild.channels, key=lambda x: x.position):
        if isinstance(c, discord.CategoryChannel):
            continue
        if isinstance(c, discord.ForumChannel):
            kind = "forum"
        elif isinstance(c, discord.StageChannel):
            kind = "stage"
        elif isinstance(c, discord.VoiceChannel):
            kind = "voice"
        elif isinstance(c, discord.TextChannel):
            kind = "news" if c.is_news() else "text"
        else:
            continue
        d = {
            "id": c.id,
            "name": c.name,
            "kind": kind,
            "category_id": c.category_id,
            "position": c.position,
            "overwrites": _overwrites(c),
        }
        if kind in ("text", "news", "forum"):
            d["topic"] = c.topic
            d["slowmode"] = c.slowmode_delay
            d["nsfw"] = c.nsfw
        if kind in ("voice", "stage"):
            d["bitrate"] = c.bitrate
            d["user_limit"] = c.user_limit
        snap["channels"].append(d)

    for e in guild.emojis:
        if e.managed:
            continue
        try:
            data = await e.read()
        except discord.HTTPException:
            continue
        snap["emojis"].append({"name": e.name, "animated": e.animated, "image": _b64(data)})

    return snap


# --------------------------------------------------------------- reporters
class InteractionReporter:
    """Edits the panel message. Only works for 15 minutes after the interaction."""

    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction

    async def update(self, embed: discord.Embed):
        try:
            await self.interaction.edit_original_response(embed=embed, view=None)
        except discord.HTTPException:
            pass


class DMReporter:
    def __init__(self, user: discord.abc.User):
        self.user = user
        self.message: discord.Message | None = None

    async def update(self, embed: discord.Embed):
        try:
            if self.message is None:
                self.message = await self.user.send(embed=embed)
            else:
                await self.message.edit(embed=embed)
        except discord.HTTPException:
            pass


# ------------------------------------------------------------------- apply
def _embed(title, steps, order, notes=None, color=None):
    lines = [f"> {LABELS[k]} | {steps[k]}" for k in order]
    if notes:
        lines += [""] + notes
    return discord.Embed(
        title=title,
        description="\n".join(lines),
        color=color or discord.Color.blurple(),
    )


async def _make_channel(target: discord.Guild, ch: dict, category, ow: dict):
    kind = ch["kind"]
    common = dict(name=ch["name"], category=category, overwrites=ow, reason=REASON)

    if kind in ("text", "news"):
        kw = dict(topic=ch.get("topic"), slowmode_delay=ch.get("slowmode", 0), nsfw=ch.get("nsfw", False))
        if kind == "news":
            try:
                return await target.create_text_channel(**common, **kw, news=True)
            except discord.HTTPException:
                pass
        return await target.create_text_channel(**common, **kw)

    if kind == "voice":
        return await target.create_voice_channel(
            **common,
            bitrate=min(ch.get("bitrate", 64000), target.bitrate_limit),
            user_limit=ch.get("user_limit", 0),
        )

    if kind == "stage":
        try:
            return await target.create_stage_channel(**common)
        except discord.HTTPException:
            return await target.create_voice_channel(**common)

    if kind == "forum":
        try:
            return await target.create_forum(
                **common,
                topic=ch.get("topic"),
                slowmode_delay=ch.get("slowmode", 0),
                nsfw=ch.get("nsfw", False),
            )
        except discord.HTTPException:
            return await target.create_text_channel(
                **common, topic=ch.get("topic"), slowmode_delay=ch.get("slowmode", 0), nsfw=ch.get("nsfw", False)
            )

    return None


async def run_copy(snap: dict, selected: set, target: discord.Guild, reporter) -> bool:
    order = [k for k in OPTIONS if k in selected]
    steps = {k: "waiting" for k in order}
    errors: list[str] = []
    stats: dict[str, str] = {}
    role_map = {snap["source"]["id"]: target.default_role}

    async def push(title="Copying", notes=None, color=None):
        await reporter.update(_embed(title, steps, order, notes, color))

    me = target.me
    if me is None or not me.guild_permissions.administrator:
        await reporter.update(
            discord.Embed(
                title="Copy failed",
                description="> The bot needs the Administrator permission in the target server.",
                color=discord.Color.red(),
            )
        )
        return False

    # ---- settings
    async def do_settings():
        s = snap["settings"]
        kwargs = dict(
            name=s["name"],
            verification_level=discord.VerificationLevel(s["verification_level"]),
            default_notifications=discord.NotificationLevel(s["default_notifications"]),
            explicit_content_filter=discord.ContentFilter(s["explicit_content_filter"]),
            afk_timeout=s["afk_timeout"],
            system_channel_flags=discord.SystemChannelFlags(**s["flags"]),
            reason=REASON,
        )
        try:
            kwargs["preferred_locale"] = discord.Locale(s["preferred_locale"])
        except ValueError:
            pass
        await target.edit(**kwargs)

    # ---- icon
    async def do_icon():
        if not snap.get("icon"):
            stats["icon"] = "source has no icon"
            return
        await target.edit(icon=_unb64(snap["icon"]), reason=REASON)

    # ---- roles
    async def do_roles():
        top = target.me.top_role
        for r in list(target.roles):
            if r.is_default() or r.managed or r >= top:
                continue
            try:
                await r.delete(reason=REASON)
            except discord.HTTPException:
                pass
            await asyncio.sleep(0.3)

        created = 0
        for r in snap["roles"]:
            if r["default"]:
                try:
                    await target.default_role.edit(
                        permissions=discord.Permissions(r["permissions"]), reason=REASON
                    )
                except discord.HTTPException as e:
                    errors.append(f"@everyone | {e.text or e}")
                continue
            # highest role first: every new role lands under the previous one
            try:
                new = await target.create_role(
                    name=r["name"],
                    permissions=discord.Permissions(r["permissions"]),
                    colour=discord.Colour(r["color"]),
                    hoist=r["hoist"],
                    mentionable=r["mentionable"],
                    reason=REASON,
                )
                role_map[r["id"]] = new
                created += 1
            except discord.HTTPException as e:
                errors.append(f"Role {r['name']} | {e.text or e}")
            await asyncio.sleep(0.5)
        stats["roles"] = f"{created} created"

    # ---- emojis
    async def do_emojis():
        limit = target.emoji_limit
        static = sum(1 for e in target.emojis if not e.animated)
        animated = sum(1 for e in target.emojis if e.animated)
        created = skipped = 0
        for e in snap["emojis"]:
            if e["animated"]:
                if animated >= limit:
                    skipped += 1
                    continue
            elif static >= limit:
                skipped += 1
                continue
            try:
                await target.create_custom_emoji(name=e["name"], image=_unb64(e["image"]), reason=REASON)
                created += 1
                if e["animated"]:
                    animated += 1
                else:
                    static += 1
            except discord.HTTPException as ex:
                errors.append(f"Emoji {e['name']} | {ex.text or ex}")
            await asyncio.sleep(1.0)
        stats["emojis"] = f"{created} created, {skipped} skipped (limit {limit})"

    # ---- channels
    async def do_channels():
        for ch in list(target.channels):
            try:
                await ch.delete(reason=REASON)
            except discord.HTTPException:
                pass
            await asyncio.sleep(0.3)

        def build_ow(items):
            out = {}
            for it in items:
                role = role_map.get(it["role_id"])
                if role is None:
                    continue
                out[role] = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(it["allow"]), discord.Permissions(it["deny"])
                )
            return out

        cat_map = {}
        for c in snap["categories"]:
            try:
                cat_map[c["id"]] = await target.create_category(
                    name=c["name"], overwrites=build_ow(c["overwrites"]), reason=REASON
                )
            except discord.HTTPException as e:
                errors.append(f"Category {c['name']} | {e.text or e}")
            await asyncio.sleep(0.4)

        groups: dict = {}
        for ch in snap["channels"]:
            groups.setdefault(ch["category_id"], []).append(ch)

        chan_map = {}
        created = 0
        for cid in [None] + [c["id"] for c in snap["categories"]]:
            items = sorted(
                groups.get(cid, []),
                key=lambda c: (c["kind"] in ("voice", "stage"), c["position"]),
            )
            category = cat_map.get(cid) if cid is not None else None
            for ch in items:
                try:
                    new = await _make_channel(target, ch, category, build_ow(ch["overwrites"]))
                    if new is not None:
                        chan_map[ch["id"]] = new
                        created += 1
                except discord.HTTPException as e:
                    errors.append(f"Channel {ch['name']} | {e.text or e}")
                await asyncio.sleep(0.4)

        if "settings" in selected:
            s = snap["settings"]
            afk = chan_map.get(s.get("afk_channel_id"))
            sysch = chan_map.get(s.get("system_channel_id"))
            try:
                await target.edit(
                    afk_channel=afk if isinstance(afk, discord.VoiceChannel) else None,
                    system_channel=sysch if isinstance(sysch, discord.TextChannel) else None,
                    reason=REASON,
                )
            except discord.HTTPException as e:
                errors.append(f"AFK / system channel | {e.text or e}")

        stats["channels"] = f"{len(cat_map)} categories, {created} channels"

    runners = [
        ("settings", do_settings),
        ("icon", do_icon),
        ("roles", do_roles),
        ("emojis", do_emojis),
        ("channels", do_channels),
    ]

    for key, fn in runners:
        if key not in selected:
            continue
        steps[key] = "working"
        await push()
        try:
            await fn()
            steps[key] = "done"
        except Exception as e:
            log.exception("Step %s failed", key)
            steps[key] = "failed"
            errors.append(f"{LABELS[key]} | {e}")
        await push()

    notes = [f"- {LABELS[k]} | {v}" for k, v in stats.items()]
    if errors:
        notes += ["", f"- Problems | {len(errors)}"]
        notes += [f"- {e[:120]}" for e in errors[:8]]
    await push("Copy finished", notes, discord.Color.green() if not errors else discord.Color.orange())
    return True
