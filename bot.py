"""
ELITE SYSTEM - Giveaway bot  (v2)
=================================
Commands (need Manage Server):
    !gstart <duration> <winners> <prize>   start a giveaway (mention a role to require it)
    !gend <message_id>                     end it now
    !greroll <message_id> [count]          pick new winner(s)
    !gcancel <message_id>                  cancel a giveaway (no winners, message deleted)
    !glist                                 active giveaways
    !ghelp                                 this list

What's new in v2
    - The entries count is a BIG heading on the giveaway (no more tiny footer text).
    - Cleaner layout: host avatar on top, winners shown with the first winner's avatar once it ended.
    - The winner message is an EMBED: "Congratulations", the winner big, their avatar, a link back to the
      giveaway. No emoji. One embed per winner when there are several.
    - Winners get a DM (set DM_WINNERS=0 to turn it off).
    - At the end the bot checks every pick again: people who LEFT the server, or lost the required role,
      can't win (the next entry is drawn instead).
    - Reroll posts the same winner embeds and updates the giveaway message.
    - New !gcancel. !glist and !ghelp are embeds. Nicer "you're in / you left" replies.
    - Fixed: prize longer than Discord's embed title limit crashed the giveaway, one failing giveaway
      could stop the checker loop, and old ended giveaways piled up in giveaways.json forever
      (they are kept 30 days so they can still be rerolled).

Files next to this one:  banner.gif  (optional picture shown inside the giveaway)
Railway: add a Volume at /data and set DATA_FILE=/data/giveaways.json so giveaways survive redeploys.
The Message Content intent must be enabled in the Developer Portal (prefix commands).
"""

import os
import re
import json
import time
import random
import traceback
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")
# On Railway, mount a volume (e.g. /data) and set DATA_FILE=/data/giveaways.json
# so giveaways survive redeploys.
DATA_FILE = os.getenv("DATA_FILE", "giveaways.json")
COLOR = 0x7C3AED       # purple
END_COLOR = 0x3B1F66   # darker purple when ended
WIN_COLOR = 0x8B5CF6   # winner embeds
BANNER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.gif")
BANNER_NAME = "banner.gif"
GIFT_EMOJI = discord.PartialEmoji(name="ELTgift", id=1557224616861106186)

DM_WINNERS = os.getenv("DM_WINNERS", "1") != "0"   # send winners a DM
KEEP_ENDED_DAYS = 30                                # ended giveaways stay rerollable this long
MAX_PRIZE_LEN = 200
FOOTER = "ELITE SYSTEM"

giveaways: dict[str, dict] = {}


# ---------- storage ----------
def load():
    global giveaways
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            giveaways = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        giveaways = {}


def save():
    try:
        os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(giveaways, f)
        os.replace(tmp, DATA_FILE)   # never leaves a half-written file behind
    except OSError as e:
        print(f"Couldn't save {DATA_FILE}: {e}")


# ---------- helpers ----------
def parse_duration(text: str) -> Optional[int]:
    """'1d2h30m', '45m', '90s' -> seconds"""
    text = text.lower().strip()
    matches = re.findall(r"(\d+)([smhdw])", text)
    if not matches or re.sub(r"\d+[smhdw]", "", text):
        return None
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    total = sum(int(n) * mult[u] for n, u in matches)
    return total if total > 0 else None


def note(text: str, color: int = COLOR) -> discord.Embed:
    """A small purple embed for short replies."""
    return discord.Embed(description=text, color=color)


def entries_heading(n: int) -> str:
    """The big entries line on the giveaway."""
    return f"## {n:,} {'Entry' if n == 1 else 'Entries'}"


def build_embed(g: dict) -> discord.Embed:
    ended = g["ended"]
    n = len(g["entries"])
    e = discord.Embed(title=g["prize"], color=END_COLOR if ended else COLOR)

    if os.path.exists(BANNER_FILE):
        e.set_image(url=f"attachment://{BANNER_NAME}")

    e.set_author(
        name="GIVEAWAY ENDED" if ended else "GIVEAWAY",
        icon_url=g.get("host_avatar") or None,
    )

    lines = []
    if ended:
        winners = g.get("winner_ids", [])
        lines.append(f"**Hosted by**  <@{g['host_id']}>")
        lines.append(f"**Ended**  <t:{g.get('ended_ts', g['end_ts'])}:R>")
        lines.append("")
        if winners:
            lines.append(f"**{'Winner' if len(winners) == 1 else 'Winners'}**")
            lines.extend(f"<@{i}>" for i in winners)
        else:
            lines.append("**No valid entries**")
        avatars = g.get("winner_avatars") or []
        if avatars:
            e.set_thumbnail(url=avatars[0])
    else:
        lines.append("Click the button below to enter!")
        lines.append("")
        lines.append(f"**Hosted by**  <@{g['host_id']}>")
        lines.append(f"**Winners**  {g['winners']}")
        if g.get("required_role"):
            lines.append(f"**Required role**  <@&{g['required_role']}>")
        lines.append(f"**Ends**  <t:{g['end_ts']}:R>  (<t:{g['end_ts']}:f>)")

    lines.append("")
    lines.append(entries_heading(n))
    e.description = "\n".join(lines)

    e.set_footer(text=FOOTER)
    e.timestamp = datetime.fromtimestamp(g.get("ended_ts", g["end_ts"]) if ended else g["end_ts"], tz=timezone.utc)
    return e


def jump_url(g: dict, mid: str) -> str:
    return f"https://discord.com/channels/{g['guild_id']}/{g['channel_id']}/{mid}"


async def resolve_user(bot: commands.Bot, guild: Optional[discord.Guild], uid: int):
    """Member if we have them, otherwise the plain user (needed for the avatar)."""
    user = (guild.get_member(uid) if guild else None) or bot.get_user(uid)
    if user is None:
        try:
            user = await bot.fetch_user(uid)
        except discord.HTTPException:
            return None
    return user


def avatar_url(user) -> Optional[str]:
    return user.display_avatar.with_size(256).url if user else None


async def still_valid(guild: Optional[discord.Guild], uid: int, role_id: Optional[int]) -> bool:
    """A winner must still be in the server (and still have the required role)."""
    if guild is None:
        return True
    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except discord.NotFound:
            return False
        except discord.HTTPException:
            return True   # can't check right now: don't punish the entry
    if role_id and not any(r.id == role_id for r in member.roles):
        return False
    return True


async def pick_winners(guild: Optional[discord.Guild], g: dict, count: int, exclude=()) -> list[int]:
    pool = [u for u in g["entries"] if u not in exclude]
    random.shuffle(pool)
    picked: list[int] = []
    for uid in pool:
        if len(picked) >= count:
            break
        if await still_valid(guild, uid, g.get("required_role")):
            picked.append(uid)
    return picked


# ---------- winner messages ----------
def winner_embed(user, uid: int, g: dict, url: str) -> discord.Embed:
    e = discord.Embed(
        title="Congratulations",
        description=f"## <@{uid}>\nYou won **{g['prize']}**!\n\n[View the giveaway]({url})",
        color=WIN_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    av = avatar_url(user)
    if av:
        e.set_thumbnail(url=av)
    e.set_footer(text=FOOTER)
    return e


async def announce_winners(bot: commands.Bot, guild, msg: discord.Message, g: dict, winner_ids: list[int], url: str):
    """Posts one 'Congratulations' embed per winner (with their avatar) as a reply, and DMs them."""
    users = [await resolve_user(bot, guild, uid) for uid in winner_ids]
    avatars = [avatar_url(u) for u in users]

    for start in range(0, len(winner_ids), 10):          # Discord allows 10 embeds per message
        ids = winner_ids[start:start + 10]
        embeds = [winner_embed(users[start + k], uid, g, url) for k, uid in enumerate(ids)]
        try:
            await msg.reply(
                content=" ".join(f"<@{u}>" for u in ids),    # the ping (mentions inside embeds don't notify)
                embeds=embeds,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=False),
            )
        except discord.HTTPException as e:
            print(f"Couldn't post the winner message: {e}")

    if DM_WINNERS:
        for u in users:
            if u is None or getattr(u, "bot", False):
                continue
            dm = discord.Embed(
                title="You won a giveaway",
                description=f"You won **{g['prize']}** in **{guild.name if guild else 'the server'}**.\n\n[Go to the giveaway]({url})",
                color=WIN_COLOR,
            )
            dm.set_thumbnail(url=avatar_url(u))
            dm.set_footer(text=FOOTER)
            try:
                await u.send(embed=dm)
            except (discord.Forbidden, discord.HTTPException):
                pass   # DMs closed

    return avatars


async def get_giveaway_message(bot: commands.Bot, g: dict, mid: str) -> Optional[discord.Message]:
    channel = bot.get_channel(g["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(g["channel_id"])
        except discord.HTTPException:
            return None
    try:
        return await channel.fetch_message(int(mid))
    except discord.HTTPException:
        return None


class GiveawayView(discord.ui.View):
    def __init__(self, disabled: bool = False):
        super().__init__(timeout=None)
        self.enter.disabled = disabled
        if disabled:
            self.enter.label = "Ended"

    @discord.ui.button(label="Enter", emoji=GIFT_EMOJI, style=discord.ButtonStyle.secondary, custom_id="giveaway:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = giveaways.get(str(interaction.message.id))
        if not g or g["ended"]:
            return await interaction.response.send_message(embed=note("This giveaway has ended."), ephemeral=True)

        role_id = g.get("required_role")
        if role_id and not any(r.id == role_id for r in getattr(interaction.user, "roles", [])):
            return await interaction.response.send_message(
                embed=note(f"You need <@&{role_id}> to enter this giveaway."), ephemeral=True
            )

        uid = interaction.user.id
        if uid in g["entries"]:
            g["entries"].remove(uid)
            text = "### You left the giveaway\nClick **Enter** again if you change your mind."
        else:
            g["entries"].append(uid)
            text = f"### You're in\nGood luck! You are one of **{len(g['entries']):,}** entries.\nClick **Enter** again to leave."
        save()
        await interaction.response.send_message(embed=note(text), ephemeral=True)
        try:
            await interaction.message.edit(embed=build_embed(g))
        except discord.HTTPException as e:
            print(f"Couldn't update the entry count: {e}")


async def finish(bot: commands.Bot, mid: str):
    g = giveaways.get(mid)
    if not g or g["ended"]:
        return
    g["ended"] = True                      # set first, so a second call can never finish it twice
    g["ended_ts"] = int(time.time())

    guild = bot.get_guild(g["guild_id"])
    g["winner_ids"] = await pick_winners(guild, g, g["winners"])
    users = [await resolve_user(bot, guild, uid) for uid in g["winner_ids"]]
    g["winner_avatars"] = [a for a in (avatar_url(u) for u in users) if a]
    save()

    msg = await get_giveaway_message(bot, g, mid)
    if msg is None:
        return
    try:
        await msg.edit(embed=build_embed(g), view=GiveawayView(disabled=True))
    except discord.HTTPException as e:
        print(f"Couldn't edit the ended giveaway: {e}")

    url = jump_url(g, mid)
    if g["winner_ids"]:
        await announce_winners(bot, guild, msg, g, g["winner_ids"], url)
    else:
        try:
            await msg.reply(
                embed=discord.Embed(
                    title="No winner",
                    description=f"There were no valid entries for **{g['prize']}**.\n\n[View the giveaway]({url})",
                    color=END_COLOR,
                ).set_footer(text=FOOTER),
                mention_author=False,
            )
        except discord.HTTPException:
            pass


# ---------- bot ----------
class GiveawayBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # must also be enabled in the Developer Portal
        super().__init__(command_prefix=PREFIX, intents=intents, help_command=None)

    async def setup_hook(self):
        load()
        self.add_view(GiveawayView())
        self.checker.start()

    @tasks.loop(seconds=10)
    async def checker(self):
        now = int(time.time())
        for mid, g in list(giveaways.items()):
            try:
                if not g["ended"] and g["end_ts"] <= now:
                    await finish(self, mid)
            except Exception:
                print(f"Giveaway {mid} failed to finish (will retry):")
                traceback.print_exc()

        # forget old ended giveaways (kept for a while so they can still be rerolled)
        changed = False
        for mid, g in list(giveaways.items()):
            if g["ended"]:
                if "ended_ts" not in g:
                    g["ended_ts"] = now
                    changed = True
                elif now - g["ended_ts"] > KEEP_ENDED_DAYS * 86400:
                    del giveaways[mid]
                    changed = True
        if changed:
            save()

    @checker.before_loop
    async def _wait(self):
        await self.wait_until_ready()


bot = GiveawayBot()


@bot.command(name="gstart", aliases=["giveaway"])
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def gstart(ctx: commands.Context, duration: str, winners: int, *, prize: str):
    """!gstart 1h 2 Discord Nitro   (mention a role to require it)"""
    secs = parse_duration(duration)
    if not secs:
        return await ctx.send(embed=note("Invalid duration. Use `30m`, `2h`, `1d12h`..."))
    if not 1 <= winners <= 20:
        return await ctx.send(embed=note("Winners must be between 1 and 20."))

    role = ctx.message.role_mentions[0] if ctx.message.role_mentions else None
    if role:
        prize = prize.replace(role.mention, "").strip()
    if not prize:
        return await ctx.send(embed=note("Please give a prize."))
    if len(prize) > MAX_PRIZE_LEN:
        return await ctx.send(embed=note(f"The prize is too long (max {MAX_PRIZE_LEN} characters)."))

    g = {
        "guild_id": ctx.guild.id,
        "channel_id": ctx.channel.id,
        "prize": prize,
        "host_id": ctx.author.id,
        "host_avatar": ctx.author.display_avatar.with_size(128).url,
        "end_ts": int(time.time()) + secs,
        "winners": winners,
        "entries": [],
        "required_role": role.id if role else None,
        "ended": False,
        "winner_ids": [],
    }
    file = discord.File(BANNER_FILE, filename=BANNER_NAME) if os.path.exists(BANNER_FILE) else None
    if file:
        msg = await ctx.send(embed=build_embed(g), view=GiveawayView(), file=file)
    else:
        msg = await ctx.send(embed=build_embed(g), view=GiveawayView())
    giveaways[str(msg.id)] = g
    save()
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass


@bot.command(name="gend")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def gend(ctx: commands.Context, message_id: int):
    """!gend <message_id>"""
    g = giveaways.get(str(message_id))
    if not g or g["guild_id"] != ctx.guild.id:
        return await ctx.send(embed=note("Giveaway not found."))
    if g["ended"]:
        return await ctx.send(embed=note(f"Already ended. Use `{PREFIX}greroll {message_id}`."))
    await finish(bot, str(message_id))


@bot.command(name="greroll")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def greroll(ctx: commands.Context, message_id: int, count: int = 1):
    """!greroll <message_id> [count]"""
    mid = str(message_id)
    g = giveaways.get(mid)
    if not g or g["guild_id"] != ctx.guild.id:
        return await ctx.send(embed=note("Giveaway not found."))
    if not g["ended"]:
        return await ctx.send(embed=note("That giveaway hasn't ended yet."))

    new = await pick_winners(ctx.guild, g, max(1, min(count, 20)), exclude=g["winner_ids"])
    if not new:
        return await ctx.send(embed=note("No more valid entries to pick from."))

    g["winner_ids"] += new
    users = [await resolve_user(bot, ctx.guild, uid) for uid in new]
    g.setdefault("winner_avatars", []).extend(a for a in (avatar_url(u) for u in users) if a)
    save()

    msg = await get_giveaway_message(bot, g, mid)
    if msg is not None:
        try:
            await msg.edit(embed=build_embed(g))
        except discord.HTTPException:
            pass
        await announce_winners(bot, ctx.guild, msg, g, new, jump_url(g, mid))
    else:
        await ctx.send(embeds=[winner_embed(u, uid, g, jump_url(g, mid)) for u, uid in zip(users, new)][:10])


@bot.command(name="gcancel")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def gcancel(ctx: commands.Context, message_id: int):
    """!gcancel <message_id>"""
    mid = str(message_id)
    g = giveaways.get(mid)
    if not g or g["guild_id"] != ctx.guild.id:
        return await ctx.send(embed=note("Giveaway not found."))
    del giveaways[mid]
    save()
    msg = await get_giveaway_message(bot, g, mid)
    if msg is not None:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
    await ctx.send(embed=note(f"Giveaway **{g['prize']}** was cancelled."))


@bot.command(name="glist")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def glist(ctx: commands.Context):
    """!glist"""
    active = [(m, g) for m, g in giveaways.items() if not g["ended"] and g["guild_id"] == ctx.guild.id]
    if not active:
        return await ctx.send(embed=note("No active giveaways."))
    e = discord.Embed(title="Active giveaways", color=COLOR)
    e.description = "\n\n".join(
        f"**{g['prize']}**\n<#{g['channel_id']}>  •  ends <t:{g['end_ts']}:R>  •  {len(g['entries']):,} entries\n`{m}`"
        for m, g in active
    )[:4000]
    e.set_footer(text=FOOTER)
    await ctx.send(embed=e)


@bot.command(name="ghelp")
async def ghelp(ctx: commands.Context):
    e = discord.Embed(title="Giveaway commands", color=COLOR)
    e.description = (
        f"`{PREFIX}gstart <duration> <winners> <prize>`\nStart a giveaway. Mention a role to require it.\n\n"
        f"`{PREFIX}gend <message_id>`\nEnd it now.\n\n"
        f"`{PREFIX}greroll <message_id> [count]`\nPick new winner(s).\n\n"
        f"`{PREFIX}gcancel <message_id>`\nCancel a giveaway.\n\n"
        f"`{PREFIX}glist`\nActive giveaways."
    )
    e.set_footer(text=FOOTER)
    await ctx.send(embed=e)


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=note("You need **Manage Server** to use this."))
    elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        await ctx.send(embed=note(f"Wrong usage. Try `{PREFIX}ghelp`."))
    elif isinstance(error, (commands.CommandNotFound, commands.NoPrivateMessage)):
        return
    else:
        traceback.print_exception(type(error), error, error.__traceback__)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print(f"{sum(1 for g in giveaways.values() if not g['ended'])} active giveaway(s) loaded from {DATA_FILE}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set.")
    bot.run(TOKEN)
