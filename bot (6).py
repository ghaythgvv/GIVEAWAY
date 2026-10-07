import os
import re
import json
import time
import random
from typing import Optional

import discord
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")
# On Railway, mount a volume (e.g. /data) and set DATA_FILE=/data/giveaways.json
# so giveaways survive redeploys.
DATA_FILE = os.getenv("DATA_FILE", "giveaways.json")
COLOR = 0x5865F2
END_COLOR = 0x2B2D31

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
    os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(giveaways, f)


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


def build_embed(g: dict) -> discord.Embed:
    ended = g["ended"]
    e = discord.Embed(title=f"🎉 {g['prize']}", color=END_COLOR if ended else COLOR)
    lines = [f"**Hosted by:** <@{g['host_id']}>", f"**Winners:** {g['winners']}"]
    if g.get("required_role"):
        lines.append(f"**Required role:** <@&{g['required_role']}>")
    if ended:
        w = g.get("winner_ids", [])
        lines.append("**Ended** — " + (", ".join(f"<@{i}>" for i in w) if w else "no valid entries"))
    else:
        lines.append(f"**Ends:** <t:{g['end_ts']}:R> (<t:{g['end_ts']}:f>)")
        lines.append("Click the button below to enter!")
    e.description = "\n".join(lines)
    e.set_footer(text=f"{len(g['entries'])} entries")
    return e


class GiveawayView(discord.ui.View):
    def __init__(self, disabled: bool = False):
        super().__init__(timeout=None)
        self.enter.disabled = disabled

    @discord.ui.button(label="Enter", emoji="🎉", style=discord.ButtonStyle.primary, custom_id="giveaway:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = giveaways.get(str(interaction.message.id))
        if not g or g["ended"]:
            return await interaction.response.send_message("This giveaway has ended.", ephemeral=True)

        role_id = g.get("required_role")
        if role_id and not any(r.id == role_id for r in getattr(interaction.user, "roles", [])):
            return await interaction.response.send_message(
                f"You need <@&{role_id}> to enter this giveaway.", ephemeral=True
            )

        uid = interaction.user.id
        if uid in g["entries"]:
            g["entries"].remove(uid)
            msg = "You left the giveaway."
        else:
            g["entries"].append(uid)
            msg = "You're in! Good luck 🍀 (click again to leave)"
        save()
        await interaction.response.send_message(msg, ephemeral=True)
        await interaction.message.edit(embed=build_embed(g))


def pick_winners(g: dict, count: int, exclude: list[int] = ()) -> list[int]:
    pool = [u for u in g["entries"] if u not in exclude]
    return random.sample(pool, min(count, len(pool)))


async def finish(bot: commands.Bot, mid: str):
    g = giveaways.get(mid)
    if not g or g["ended"]:
        return
    g["ended"] = True
    g["winner_ids"] = pick_winners(g, g["winners"])
    save()

    channel = bot.get_channel(g["channel_id"])
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(int(mid))
    except discord.NotFound:
        return
    await msg.edit(embed=build_embed(g), view=GiveawayView(disabled=True))
    if g["winner_ids"]:
        mentions = ", ".join(f"<@{i}>" for i in g["winner_ids"])
        await msg.reply(f"🎊 Congratulations {mentions}! You won **{g['prize']}**!")
    else:
        await msg.reply(f"No valid entries for **{g['prize']}**.")


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
            if not g["ended"] and g["end_ts"] <= now:
                await finish(self, mid)

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
        return await ctx.send("Invalid duration. Use `30m`, `2h`, `1d12h`...")
    if not 1 <= winners <= 20:
        return await ctx.send("Winners must be between 1 and 20.")

    role = ctx.message.role_mentions[0] if ctx.message.role_mentions else None
    if role:
        prize = prize.replace(role.mention, "").strip()
    if not prize:
        return await ctx.send("Please give a prize.")

    g = {
        "guild_id": ctx.guild.id,
        "channel_id": ctx.channel.id,
        "prize": prize,
        "host_id": ctx.author.id,
        "end_ts": int(time.time()) + secs,
        "winners": winners,
        "entries": [],
        "required_role": role.id if role else None,
        "ended": False,
        "winner_ids": [],
    }
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
        return await ctx.send("Giveaway not found.")
    if g["ended"]:
        return await ctx.send("Already ended. Use `!greroll`.")
    await finish(bot, str(message_id))


@bot.command(name="greroll")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def greroll(ctx: commands.Context, message_id: int, count: int = 1):
    """!greroll <message_id> [count]"""
    g = giveaways.get(str(message_id))
    if not g or g["guild_id"] != ctx.guild.id:
        return await ctx.send("Giveaway not found.")
    if not g["ended"]:
        return await ctx.send("That giveaway hasn't ended yet.")
    new = pick_winners(g, max(1, min(count, 20)), exclude=g["winner_ids"])
    if not new:
        return await ctx.send("No more entries to pick from.")
    g["winner_ids"] += new
    save()
    await ctx.send(f"🔁 New winner(s) for **{g['prize']}**: " + ", ".join(f"<@{i}>" for i in new))


@bot.command(name="glist")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def glist(ctx: commands.Context):
    """!glist"""
    active = [(m, g) for m, g in giveaways.items() if not g["ended"] and g["guild_id"] == ctx.guild.id]
    if not active:
        return await ctx.send("No active giveaways.")
    await ctx.send("\n".join(
        f"• **{g['prize']}** — <#{g['channel_id']}> — ends <t:{g['end_ts']}:R> — `{m}`" for m, g in active
    ))


@bot.command(name="ghelp")
async def ghelp(ctx: commands.Context):
    await ctx.send(
        f"**Giveaway commands**\n"
        f"`{PREFIX}gstart <duration> <winners> <prize>` — start (mention a role to require it)\n"
        f"`{PREFIX}gend <message_id>` — end now\n"
        f"`{PREFIX}greroll <message_id> [count]` — pick new winner(s)\n"
        f"`{PREFIX}glist` — active giveaways"
    )


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Manage Server** to use this.")
    elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        await ctx.send(f"Wrong usage. Try `{PREFIX}ghelp`.")
    elif isinstance(error, (commands.CommandNotFound, commands.NoPrivateMessage)):
        return
    else:
        raise error


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


if __name__ == "__main__":
    bot.run(TOKEN)
