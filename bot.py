"""
ELITE SYSTEM - Giveaway bot  (v3)
=================================
Commands (need Manage Server):
    !gstart <duration> <winners> <prize>   start a giveaway (mention a role to require it)
    !gend <message_id>                     end it now
    !greroll <message_id> [count]          pick new winner(s)
    !gcancel <message_id>                  cancel a giveaway (no winners, message deleted)
    !glist                                 active giveaways
    !ghelp                                 this list

What's new in v3
    - The giveaway is ONE card (Discord "Components V2"): title, banner, details, the big entries
      number AND the Enter button are all inside the same purple card.
    - ONE button inside the card: "Enter / Leave". Press it to enter, press it again to leave. Silent:
      no private messages, the entries number on the card just updates. The label is the same for
      every member on purpose (Discord can't show a different label per person).
    - Everything from v2 is kept: winner embeds with avatars, DMs, re-check at the end,
      reroll, cancel, 30-day archive.

REQUIRES  discord.py >= 2.6   (requirements.txt:  discord.py>=2.6)
Giveaways created with v2 keep working for winners/DMs/rerolls, but their old message can't be turned
into the new card - end or cancel them before you deploy v3.

Files next to this one:  banner.gif  (optional picture shown inside the card)
Railway: add a Volume at /data and set DATA_FILE=/data/giveaways.json so giveaways survive redeploys.
The Message Content intent must be enabled in the Developer Portal (prefix commands).
"""

import os
import re
import json
import time
import random
import traceback
from typing import Optional

import discord
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")
DATA_FILE = os.getenv("DATA_FILE", "giveaways.json")
COLOR = 0x7C3AED       # purple
END_COLOR = 0x3B1F66   # darker purple when ended
WIN_COLOR = 0x8B5CF6   # winner embeds
BANNER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.gif")
BANNER_NAME = "banner.gif"
GIFT_EMOJI = discord.PartialEmoji(name="ELTgift", id=1557224616861106186)

DM_WINNERS = os.getenv("DM_WINNERS", "1") != "0"
KEEP_ENDED_DAYS = 30
MAX_PRIZE_LEN = 200
# "static" = (default) the one button always says "Enter / Leave", correct for everyone
# "flip"   = the label follows the LAST person who clicked, so it is wrong for everyone else
BUTTON_MODE = os.getenv("BUTTON_MODE", "static")
FOOTER = "ELITE SYSTEM"
NO_PINGS = discord.AllowedMentions.none()   # the card mentions people, it must never ping them

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
        os.replace(tmp, DATA_FILE)
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
    return f"## {n:,} {'Entry' if n == 1 else 'Entries'}"


def jump_url(g: dict, mid: str) -> str:
    return f"https://discord.com/channels/{g['guild_id']}/{g['channel_id']}/{mid}"


async def resolve_user(bot: commands.Bot, guild: Optional[discord.Guild], uid: int):
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
            return True
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


# ---------- the giveaway card ----------
class EnterRow(discord.ui.ActionRow):
    """ONE button inside the card. Press it to enter, press it again to leave."""

    def __init__(self, disabled: bool = False, leaving: bool = False):
        super().__init__()
        if disabled:
            self.toggle.disabled = True
            self.toggle.label = "Ended"
            self.toggle.emoji = None
        elif BUTTON_MODE == "static":
            self.toggle.label = "Enter / Leave"
        elif leaving:
            self.toggle.label = "Leave"
            self.toggle.emoji = None
            self.toggle.style = discord.ButtonStyle.secondary

    @discord.ui.button(label="Enter", emoji=GIFT_EMOJI, style=discord.ButtonStyle.primary, custom_id="giveaway:enter")
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_entry(interaction)


class GiveawayView(discord.ui.LayoutView):
    """One purple card: title, banner, details, big entries number, Enter button."""

    def __init__(self, g: Optional[dict] = None):
        super().__init__(timeout=None)
        ended = bool(g and g["ended"])
        kids: list = []

        if g:
            T = discord.ui.TextDisplay
            sep = lambda: discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small)

            # --- header: label + prize, with an avatar on the right
            thumb = None
            if ended and g.get("winner_avatars"):
                thumb = g["winner_avatars"][0]        # first winner once it's over
            elif g.get("host_avatar"):
                thumb = g["host_avatar"]               # the host while it runs
            tag = "GIVEAWAY ENDED" if ended else "GIVEAWAY"
            header = [T(f"-# {tag}"), T(f"# {g['prize']}")]
            kids.append(discord.ui.Section(*header, accessory=discord.ui.Thumbnail(thumb)) if thumb else header[0])
            if not thumb:
                kids.append(header[1])

            # --- banner
            if os.path.exists(BANNER_FILE):
                kids.append(discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{BANNER_NAME}")))

            kids.append(sep())

            # --- details
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
            else:
                lines.append("Press the button below to enter!")
                lines.append("")
                lines.append(f"**Hosted by**  <@{g['host_id']}>")
                lines.append(f"**Winners**  {g['winners']}")
                if g.get("required_role"):
                    lines.append(f"**Required role**  <@&{g['required_role']}>")
                lines.append(f"**Ends**  <t:{g['end_ts']}:R>  (<t:{g['end_ts']}:f>)")
            kids.append(T("\n".join(lines)))

            kids.append(sep())
            kids.append(T(entries_heading(len(g["entries"]))))

        kids.append(EnterRow(disabled=ended, leaving=bool(g and g.get("last_in"))))
        if g:
            kids.append(discord.ui.TextDisplay(f"-# {FOOTER}"))

        self.add_item(discord.ui.Container(*kids, accent_color=END_COLOR if ended else COLOR))


async def refresh(msg: Optional[discord.Message], g: dict):
    """Re-draw the giveaway card (entries count, winners, ended state)."""
    if msg is None or not g.get("v3"):
        return   # giveaways made with v2 keep their old message
    try:
        await msg.edit(view=GiveawayView(g), allowed_mentions=NO_PINGS)
    except discord.HTTPException as e:
        print(f"Couldn't update the giveaway card: {e}")


# ---------- entering / leaving ----------
def has_role(user, role_id: Optional[int]) -> bool:
    return not role_id or any(r.id == role_id for r in getattr(user, "roles", []))


async def toggle_entry(interaction: discord.Interaction):
    """Button pressed: not in -> enter, already in -> leave. Silent, the card just updates."""
    mid = str(interaction.message.id)
    g = giveaways.get(mid)
    if not g or g["ended"]:
        return await interaction.response.send_message(embed=note("This giveaway has ended."), ephemeral=True)

    uid = interaction.user.id
    if uid in g["entries"]:
        g["entries"].remove(uid)
        g["last_in"] = False
    else:
        if not has_role(interaction.user, g.get("required_role")):
            return await interaction.response.send_message(
                embed=note(f"You need <@&{g['required_role']}> to enter this giveaway."), ephemeral=True
            )
        g["entries"].append(uid)
        g["last_in"] = True
    save()

    if g.get("v3"):
        await interaction.response.edit_message(view=GiveawayView(g), allowed_mentions=NO_PINGS)
    else:   # giveaway made with v2: its message can't be redrawn as a card
        text = "### You're in" if g["last_in"] else "### You left the giveaway"
        await interaction.response.send_message(embed=note(text), ephemeral=True)


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
    """One 'Congratulations' embed per winner (with their avatar) as a reply, plus a DM."""
    users = [await resolve_user(bot, guild, uid) for uid in winner_ids]

    for start in range(0, len(winner_ids), 10):          # Discord allows 10 embeds per message
        ids = winner_ids[start:start + 10]
        embeds = [winner_embed(users[start + k], uid, g, url) for k, uid in enumerate(ids)]
        try:
            await msg.reply(
                content=" ".join(f"<@{u}>" for u in ids),    # the ping
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
                pass


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
    await refresh(msg, g)

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
        intents.message_content = True
        super().__init__(command_prefix=PREFIX, intents=intents, help_command=None)

    async def setup_hook(self):
        load()
        self.add_view(GiveawayView())      # keeps the in-card Enter button alive after restarts
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
        "v3": True,                       # card format (Components V2)
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
    kwargs = {"view": GiveawayView(g), "allowed_mentions": NO_PINGS}
    if os.path.exists(BANNER_FILE):
        kwargs["file"] = discord.File(BANNER_FILE, filename=BANNER_NAME)
    msg = await ctx.send(**kwargs)
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
        await refresh(msg, g)
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
