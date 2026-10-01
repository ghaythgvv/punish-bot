"""
ELT Punishment Bot
=======================================
Slash commands (/warn, /mute, /timeout, /kick, /ban) so staff get Discord's own
UI — a member picker, typed fields, and built-in validation — instead of typing
raw text commands. Wraps punishment_gif.py's render_card_gif() to post an
ANIMATED punishment card with the target's own avatar and display name, plus a
"View Punishment Details" button underneath. If the animated card can't be made
for any reason, it falls back to the still PNG card from punishment_card.py.

Who can use the commands: server Administrators, anyone with one of the staff
roles in STAFF_ROLE_IDS (see CONFIG), or anyone who has the matching Discord
permission (Manage Messages / Timeout Members / Kick Members / Ban Members).

Requirements:
    pip install discord.py Pillow     (discord.py 2.4 or newer)

Before running:
    - Put punishment_card.py, punishment_gif.py and card_bg.gif in the same
      folder as this file, with the fonts/ folder next to them.
    - Enable SERVER MEMBERS INTENT for your bot in the Discord Developer Portal.
    - Invite the bot with BOTH the "bot" and "applications.commands" scopes.
    - Put your token in the DISCORD_TOKEN environment variable (Railway Variables).
    - Optional: add your staff role IDs in a Railway variable called
      STAFF_ROLE_IDS (comma separated, e.g. 111,222,333).
    - IMPORTANT: the bot's own role must sit ABOVE the highest role of anyone you
      want to mute/timeout/kick/ban, or Discord rejects it with a 403.
"""

import io
import os
import re
import asyncio
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from punishment_card import render_card, TYPE_STYLE
from punishment_gif import render_card_gif, clean_for_card, MAX_BYTES

# =========================== CONFIG ===========================
TOKEN = os.environ.get("DISCORD_TOKEN")

GUILD_ID = 1410440666747633707  # ELT server ID — commands sync straight to this guild so they show up instantly

PUNISHMENT_LOG_CHANNEL_ID = None  # channel where punishment cards get posted (None = the channel the command was run in)

# Staff role IDs allowed to use every punishment command (comma separated in the
# STAFF_ROLE_IDS Railway variable). Add your "mod" and "good" role IDs there.
STAFF_ROLE_IDS = {
    int(x)
    for x in os.environ.get("STAFF_ROLE_IDS", "1513904136783925380").replace(" ", "").split(",")
    if x.isdigit()
}
# ================================================================

intents = discord.Intents.default()
intents.members = True  # needed to look up/timeout/kick/ban members and read their avatar

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

_BOLD_UPPER_START = 0x1D5D4  # Mathematical Sans-Serif Bold Capital A
_BOLD_LOWER_START = 0x1D5EE  # Mathematical Sans-Serif Bold Small a
_BOLD_DIGIT_START = 0x1D7EC  # Mathematical Sans-Serif Bold Digit Zero


def bold(text: str) -> str:
    """Converts A-Z, a-z, 0-9 to Mathematical Sans-Serif Bold. Never run this over
    text containing a Discord ID (<@id>, <#id>, <:name:id>, <t:unix:R>)."""
    if not text:
        return text
    out = []
    for ch in text:
        code = ord(ch)
        if 65 <= code <= 90:
            out.append(chr(_BOLD_UPPER_START + (code - 65)))
        elif 97 <= code <= 122:
            out.append(chr(_BOLD_LOWER_START + (code - 97)))
        elif 48 <= code <= 57:
            out.append(chr(_BOLD_DIGIT_START + (code - 48)))
        else:
            out.append(ch)
    return "".join(out)


# Case numbers and full punishment details, in memory only (reset on restart).
case_counter = 0
punishment_records = {}


def card_name(member: discord.abc.User) -> str:
    """Name shown on the card: the server display name, or the plain username if
    the display name has nothing the card font can draw."""
    return clean_for_card(member.display_name) or member.name


def audit_reason(reason: str, action: str, by: discord.abc.User) -> str:
    """Reason text for Discord's audit log (max 512 characters)."""
    return f"{reason} — {action} by {by}"[:512]


def staff_only(permission: str):
    """Lets through Administrators, members with a role in STAFF_ROLE_IDS, and
    members who have the matching Discord permission."""

    async def predicate(interaction: discord.Interaction) -> bool:
        user = interaction.user
        perms = user.guild_permissions
        if perms.administrator or getattr(perms, permission, False):
            return True
        if any(role.id in STAFF_ROLE_IDS for role in user.roles):
            return True
        raise app_commands.MissingPermissions([permission])

    return app_commands.check(predicate)


def target_problem(interaction: discord.Interaction, member: discord.Member, needs_bot_rank: bool = True):
    """Returns a message if this punishment shouldn't go ahead, else None."""
    guild = interaction.guild
    if member.id == interaction.user.id:
        return "You can't punish yourself."
    if member.id == bot.user.id:
        return "I can't punish myself."
    if member.id == guild.owner_id:
        return "You can't punish the server owner."
    if interaction.user.id != guild.owner_id and member.top_role >= interaction.user.top_role:
        return "That member's highest role is equal to or above yours."
    if needs_bot_rank and member.top_role >= guild.me.top_role:
        return "That member's highest role is equal to or above mine — move my role higher."
    return None


async def send_with_retry(channel, **kwargs):
    """Send a message with exponential backoff retry logic for rate limits."""
    max_attempts = 5
    backoff_delays = [1, 2, 4, 8, 16]

    for attempt in range(max_attempts):
        try:
            return await channel.send(**kwargs)
        except discord.errors.HTTPException as e:
            if e.status == 429:
                if attempt < max_attempts - 1:
                    delay = backoff_delays[attempt]
                    print(f"⏳ Rate limited, retrying in {delay}s (attempt {attempt + 1}/{max_attempts})")
                    await asyncio.sleep(delay)
                else:
                    print(f"❌ Failed to send message after {max_attempts} attempts")
                    raise
            else:
                raise


class PunishmentDetailsButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"punishment_details_(?P<case>[0-9]+)",
):
    """'View Punishment Details' button. Being a DynamicItem, it keeps working
    after a bot restart instead of showing 'This interaction failed'."""

    def __init__(self, case_no: int):
        super().__init__(
            discord.ui.Button(
                label=f"🔍 {bold('View Punishment Details')}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"punishment_details_{case_no}",
            )
        )
        self.case_no = case_no

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(int(match["case"]))

    async def callback(self, interaction: discord.Interaction):
        record = punishment_records.get(self.case_no)
        if record is None:
            return await interaction.response.send_message(
                bold("Details for this case aren't available anymore (the bot restarted since it was issued)."),
                ephemeral=True,
            )

        color_rgb = TYPE_STYLE[record["type"]][0]
        embed = discord.Embed(
            title=bold(f"Case #{self.case_no:04d} — {record['type'].title()}"),
            color=discord.Color.from_rgb(*color_rgb),
        )
        embed.add_field(name=bold("User"), value=f"<@{record['user_id']}> ({bold(record['user_tag'])})", inline=False)
        embed.add_field(name=bold("Punisher"), value=f"<@{record['punisher_id']}> ({bold(record['punisher_tag'])})", inline=False)
        embed.add_field(name=bold("Reason"), value=bold(record["reason"]), inline=False)
        embed.add_field(name=bold("Date"), value=bold(record["date_text"]), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def issue_punishment(interaction: discord.Interaction, member: discord.Member, ptype: str, reason: str):
    """Renders the card with the member's own avatar and posts it with the details
    button. Assumes the interaction has already been deferred (ephemeral)."""
    global case_counter
    case_counter += 1
    case_no = case_counter

    try:
        avatar_bytes = await member.display_avatar.replace(size=256, format="png").read()
    except Exception as e:
        print(f"⚠️ Couldn't fetch avatar for {member}: {e}")
        avatar_bytes = None

    date_text = discord.utils.utcnow().strftime("%d/%m/%Y")
    user_name, punisher_name = card_name(member), card_name(interaction.user)
    max_bytes = min(MAX_BYTES, int(interaction.guild.filesize_limit * 0.9))

    # CPU-bound Pillow work runs in a thread so it doesn't block the event loop.
    try:
        card_bytes = await asyncio.to_thread(
            render_card_gif, user_name, punisher_name, reason, ptype, case_no, date_text, avatar_bytes, max_bytes
        )
        ext = "gif"
    except Exception as e:
        print(f"⚠️ Animated card failed ({type(e).__name__}: {e}) — falling back to the still card")
        try:
            card_bytes = await asyncio.to_thread(
                render_card, user_name, punisher_name, reason, ptype, case_no, date_text, avatar_bytes
            )
            ext = "png"
        except Exception as e2:
            print(f"❌ Failed to render punishment card: {e2}")
            await interaction.followup.send(f"⚠️ {bold('Something went wrong generating the punishment card.')}", ephemeral=True)
            return

    punishment_records[case_no] = {
        "user_id": member.id,
        "user_tag": str(member),
        "punisher_id": interaction.user.id,
        "punisher_tag": str(interaction.user),
        "reason": reason,
        "type": ptype,
        "date_text": date_text,
    }

    log_channel = interaction.guild.get_channel(PUNISHMENT_LOG_CHANNEL_ID) if PUNISHMENT_LOG_CHANNEL_ID else interaction.channel
    if log_channel is None:
        log_channel = interaction.channel

    file = discord.File(io.BytesIO(card_bytes), filename=f"punishment_case_{case_no:04d}.{ext}")
    view = discord.ui.View(timeout=None)
    view.add_item(PunishmentDetailsButton(case_no))

    try:
        await send_with_retry(log_channel, file=file, view=view)
        print(f"✅ Case #{case_no:04d} ({ptype}) posted for {member} by {interaction.user}")
    except Exception as e:
        print(f"❌ Failed to send punishment card: {e}")
        await interaction.followup.send(
            f"⚠️ {bold('Generated the card but could not post it — check my permissions in that channel.')}",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"✅ {bold(f'{ptype.title()} logged for')} {member.mention} {bold('in')} {log_channel.mention}.",
        ephemeral=True,
    )


async def reject(interaction: discord.Interaction, command: str, target: discord.Member, msg: str):
    """Sends the ephemeral rejection AND logs it so it shows up in Railway's logs."""
    print(f"🚫 /{command}: {interaction.user} -> {target} blocked: {msg}")
    await interaction.followup.send(f"⚠️ {bold(msg)}", ephemeral=True)


@bot.event
async def setup_hook():
    bot.add_dynamic_items(PunishmentDetailsButton)
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"🔄 Synced {len(synced)} slash command(s) to guild {GUILD_ID}")
    except Exception as e:
        print(f"❌ Failed to sync slash commands: {e}")


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"👮 Staff role IDs: {sorted(STAFF_ROLE_IDS) or 'none set'}")


@bot.tree.command(name="warn", description="Log a warning card for a member. No Discord action is taken.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="The member being warned", reason="Why they're being warned")
@app_commands.guild_only()
@staff_only("manage_messages")
async def warn_cmd(interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 300]):
    await interaction.response.defer(ephemeral=True)
    problem = target_problem(interaction, member, needs_bot_rank=False)
    if problem:
        return await reject(interaction, "warn", member, problem)
    await issue_punishment(interaction, member, "WARNING", reason)


@bot.tree.command(name="mute", description="Time a member out and log a mute card.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="The member being muted", minutes="How long to mute them for, in minutes", reason="Why they're being muted")
@app_commands.guild_only()
@staff_only("moderate_members")
async def mute_cmd(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: app_commands.Range[str, 1, 300]):
    await interaction.response.defer(ephemeral=True)
    problem = target_problem(interaction, member)
    if problem:
        return await reject(interaction, "mute", member, problem)
    try:
        await member.timeout(timedelta(minutes=minutes), reason=audit_reason(reason, "muted", interaction.user))
    except discord.Forbidden:
        return await reject(interaction, "mute", member, "I do not have permission to time out that member.")
    except discord.HTTPException as e:
        print(f"❌ /mute HTTPException for {member}: {e}")
        return await interaction.followup.send(f"⚠️ {bold('Failed to mute:')} {e}", ephemeral=True)
    await issue_punishment(interaction, member, "MUTE", f"{reason} (for {minutes}m)")


@bot.tree.command(name="timeout", description="Time a member out for minutes, hours or days and log a timeout card.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="The member being timed out", amount="How long (a number)", unit="Minutes, hours or days", reason="Why they're being timed out")
@app_commands.choices(unit=[
    app_commands.Choice(name="Minutes", value="minutes"),
    app_commands.Choice(name="Hours", value="hours"),
    app_commands.Choice(name="Days", value="days"),
])
@app_commands.guild_only()
@staff_only("moderate_members")
async def timeout_cmd(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 40320], unit: app_commands.Choice[str], reason: app_commands.Range[str, 1, 300]):
    await interaction.response.defer(ephemeral=True)
    duration = timedelta(**{unit.value: amount})
    if duration > timedelta(days=28):
        return await reject(interaction, "timeout", member, "Discord only allows timeouts up to 28 days.")
    problem = target_problem(interaction, member)
    if problem:
        return await reject(interaction, "timeout", member, problem)
    try:
        await member.timeout(duration, reason=audit_reason(reason, "timed out", interaction.user))
    except discord.Forbidden:
        return await reject(interaction, "timeout", member, "I do not have permission to time out that member.")
    except discord.HTTPException as e:
        print(f"❌ /timeout HTTPException for {member}: {e}")
        return await interaction.followup.send(f"⚠️ {bold('Failed to time out:')} {e}", ephemeral=True)
    unit_label = unit.value if amount != 1 else unit.value[:-1]
    await issue_punishment(interaction, member, "TIMEOUT", f"{reason} (for {amount} {unit_label})")


@bot.tree.command(name="kick", description="Kick a member and log a kick card.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="The member being kicked", reason="Why they're being kicked")
@app_commands.guild_only()
@staff_only("kick_members")
async def kick_cmd(interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 300]):
    await interaction.response.defer(ephemeral=True)
    problem = target_problem(interaction, member)
    if problem:
        return await reject(interaction, "kick", member, problem)
    try:
        await member.kick(reason=audit_reason(reason, "kicked", interaction.user))
    except discord.Forbidden:
        return await reject(interaction, "kick", member, "I do not have permission to kick that member.")
    except discord.HTTPException as e:
        print(f"❌ /kick HTTPException for {member}: {e}")
        return await interaction.followup.send(f"⚠️ {bold('Failed to kick:')} {e}", ephemeral=True)
    await issue_punishment(interaction, member, "KICK", reason)


@bot.tree.command(name="ban", description="Ban a member and log a ban card.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(member="The member being banned", reason="Why they're being banned")
@app_commands.guild_only()
@staff_only("ban_members")
async def ban_cmd(interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 300]):
    await interaction.response.defer(ephemeral=True)
    problem = target_problem(interaction, member)
    if problem:
        return await reject(interaction, "ban", member, problem)
    try:
        await member.ban(reason=audit_reason(reason, "banned", interaction.user))
    except discord.Forbidden:
        return await reject(interaction, "ban", member, "I do not have permission to ban that member.")
    except discord.HTTPException as e:
        print(f"❌ /ban HTTPException for {member}: {e}")
        return await interaction.followup.send(f"⚠️ {bold('Failed to ban:')} {e}", ephemeral=True)
    await issue_punishment(interaction, member, "BAN", reason)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        print(f"🚫 {interaction.user} tried /{interaction.command.name if interaction.command else '?'} without permission")
        msg = f"⚠️ {bold('You do not have permission to do that.')}"
    else:
        original = getattr(error, "original", error)
        print(f"❌ Unhandled app command error in {interaction.command}: {original}")
        msg = f"⚠️ {bold('Something went wrong running that command.')}"

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Add it in Railway's Variables tab, then redeploy.")

bot.run(TOKEN)
