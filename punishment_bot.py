"""
ELT Punishment Bot  (v2)
=======================================
Slash commands: /warn /unwarn /mute /timeout /kick /ban  +  /warnings /history
Posts an ANIMATED punishment card (punishment_gif.render_card_gif) with the target's avatar and
name plus a "View Punishment Details" button. Falls back to the still PNG card if needed.

Who can use what:
    Staff (lowest role in STAFF_ROLE_IDS, or above) -> /warn  /warnings
    Moderators (MOD_ROLE_ID, or above)              -> + /unwarn /mute /timeout /kick /history
    Administrators + server owner                   -> everything, including /ban

Nobody can punish: the server owner, an Administrator, or anyone whose top role is ABOVE the
Moderator role. And nobody can punish someone whose top role is equal to or above their own.

Bans: only Administrators can /ban and only they trigger the automatic ban on the 3rd warning
(set MODS_CAN_BAN = True to let Moderators do it too). When Staff / a Moderator gives a 3rd
warning, no ban happens: the Administrators are pinged instead.
/ban also works on someone who ALREADY LEFT: paste their user ID in the member box.

What's new in v2
    - Warnings are counted under a lock, so two staff warning the same person at the same moment
      can no longer skip the 3rd warning.
    - The case database is crash-safe: a corrupt file is never overwritten (it is copied aside and the
      .bak backup is used), and the case counter can never go backwards.
    - The hourly expiry loop can no longer die silently; members who left have their expired
      warnings cleared quietly.
    - Members who leave and rejoin get their Warn role back.
    - The punished member gets a DM (kick/ban DMs are removed again if the action fails).
    - New: /warnings (active warnings + when they clear) and /history (all cases of a member).

Requirements:
    pip install discord.py Pillow     (discord.py 2.4 or newer)

Before running:
    - punishment_card.py, punishment_gif.py, card_bg.gif and the fonts/ folder next to this file.
    - SERVER MEMBERS INTENT enabled in the Developer Portal.
    - Invite with the "bot" and "applications.commands" scopes.
    - DISCORD_TOKEN env variable (Railway Variables). Optional STAFF_ROLE_IDS (comma separated).
    - The bot's role must sit ABOVE anyone you want to mute/timeout/kick/ban and above the Warn roles.
    - Railway: add a Volume mounted at /data so cases survive redeploys.
    - This bot needs its OWN Discord application + token (shared tokens overwrite each other's commands).

If the slash commands ever disappear: an admin mentions the bot and types "sync".
"""

import io
import os
import re
import json
import time
import shutil
import asyncio
import traceback
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from punishment_card import render_card, TYPE_STYLE
from punishment_gif import render_card_gif, clean_for_card, MAX_BYTES

# Own style for the green "warning cleared" card.
TYPE_STYLE.setdefault("WARNING CLEARED", ((70, 220, 120), 100))

# Serialises every /warn: counting, card style, saving and roles happen one warn at a time.
warning_lock = asyncio.Lock()

# =========================== CONFIG ===========================
TOKEN = os.environ.get("DISCORD_TOKEN")

GUILD_ID = 1410440666747633707
GUILD = discord.Object(id=GUILD_ID)

PUNISHMENT_LOG_CHANNEL_ID = None   # None = post in the channel where the command was used

MOD_ROLE_ID = 1513904125086011402
ADMIN_ROLE_ID = 1513904120803889243   # pinged when a 3rd warning needs a ban decision

MODS_CAN_BAN = False

WARN_1_ROLE_ID = 1513904153900875897
WARN_2_ROLE_ID = 1513904154719027291

WARNING_EXPIRE_DAYS = 30
MAX_WARNINGS = 3

WARNING_CLEAR_CHANNEL_ID = 1540154905644367893

DM_PUNISHED_MEMBERS = True   # send the punished member a DM with the reason

STAFF_ROLE_IDS = {
    int(x)
    for x in os.environ.get("STAFF_ROLE_IDS", "1513904136783925380").replace(" ", "").split(",")
    if x.isdigit()
}

DATA_DIR = (
    os.environ.get("DATA_DIR")
    or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or os.path.dirname(os.path.abspath(__file__))
)
DB_PATH = os.path.join(DATA_DIR, "punishments.json")
# ================================================================

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

_BOLD_UPPER_START = 0x1D5D4
_BOLD_LOWER_START = 0x1D5EE
_BOLD_DIGIT_START = 0x1D7EC


def bold(text: str) -> str:
    """A-Z, a-z, 0-9 -> Mathematical Sans-Serif Bold."""
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


def short(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


# ================================================================
# CASE NUMBERS + DATABASE (crash-safe)
# ================================================================

def _read_db_file(path: str):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    records = {int(k): v for k, v in data.get("records", {}).items()}
    return int(data.get("counter", 0)), records


def load_db():
    """Reads the database. A corrupt file is copied aside (never overwritten) and the backup is tried."""
    for path in (DB_PATH, DB_PATH + ".bak"):
        try:
            counter, records = _read_db_file(path)
            if path != DB_PATH:
                print(f"♻️ Restored the case database from {path}")
            return max(counter, max(records) if records else 0), records
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"⚠️ Couldn't read {path}: {e}")
            try:
                shutil.copyfile(path, f"{path}.corrupt-{int(time.time())}")
                print(f"📦 Kept a copy of the unreadable file as {path}.corrupt-*")
            except Exception:
                pass
    return 0, {}


def save_db():
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        tmp = DB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"counter": case_counter, "records": punishment_records}, f, ensure_ascii=False)
        if os.path.exists(DB_PATH):
            try:
                shutil.copyfile(DB_PATH, DB_PATH + ".bak")
            except Exception as e:
                print(f"⚠️ Couldn't refresh the backup: {e}")
        os.replace(tmp, DB_PATH)
    except Exception as e:
        print(f"❌ Couldn't save {DB_PATH}: {e}")


case_counter, punishment_records = load_db()
print(f"📁 Case data: {DB_PATH} (last case #{case_counter:04d}, {len(punishment_records)} saved)")


def next_case() -> int:
    global case_counter
    case_counter += 1
    save_db()
    return case_counter


def card_name(member: discord.abc.User) -> str:
    return clean_for_card(member.display_name) or member.name


def audit_reason(reason: str, action: str, by: discord.abc.User) -> str:
    return f"{reason} — {action} by {by}"[:512]


# ================================================================
# PERMISSIONS
# ================================================================
TIER_RANK = {"none": 0, "staff": 1, "mod": 2, "admin": 3}
TIER_LABEL = {"staff": "Staff", "mod": "Moderators", "admin": "Administrators"}
BAN_MIN_TIER = "mod" if MODS_CAN_BAN else "admin"


class NotAllowed(app_commands.CheckFailure):
    """The text is shown to the user."""


def actor_tier(member: discord.Member) -> str:
    guild = member.guild
    if member.id == guild.owner_id or member.guild_permissions.administrator:
        return "admin"
    mod_role = guild.get_role(MOD_ROLE_ID)
    if mod_role and member.top_role >= mod_role:
        return "mod"
    staff_roles = [r for r in (guild.get_role(i) for i in STAFF_ROLE_IDS) if r]
    if staff_roles and member.top_role >= min(staff_roles):
        return "staff"
    return "none"


def can_ban(member: discord.Member) -> bool:
    return TIER_RANK[actor_tier(member)] >= TIER_RANK[BAN_MIN_TIER]


def require_tier(min_tier: str):
    async def predicate(interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and TIER_RANK[actor_tier(interaction.user)] >= TIER_RANK[min_tier]:
            return True
        raise NotAllowed(f"This command is for {TIER_LABEL[min_tier]} or higher.")
    return app_commands.check(predicate)


def target_problem(interaction: discord.Interaction, member: discord.Member, needs_bot_rank: bool = True):
    """Returns a message if the punishment shouldn't go ahead."""
    guild = interaction.guild

    if member.id == interaction.user.id:
        return "You can't punish yourself."
    if bot.user and member.id == bot.user.id:
        return "I can't punish myself."
    if member.id == guild.owner_id:
        return "You can't punish the server owner."
    if member.guild_permissions.administrator:
        return "You can't punish an administrator."

    mod_role = guild.get_role(MOD_ROLE_ID)
    if mod_role and member.top_role > mod_role:
        return "That member is protected (their role is above Moderator)."

    if interaction.user.id != guild.owner_id and member.top_role >= interaction.user.top_role:
        return "That member's highest role is equal to or above yours."

    if needs_bot_rank and guild.me and member.top_role >= guild.me.top_role:
        return "That member's highest role is equal to or above mine — move my role higher."

    return None


def get_log_channel(interaction: discord.Interaction):
    channel = (
        interaction.guild.get_channel(PUNISHMENT_LOG_CHANNEL_ID)
        if PUNISHMENT_LOG_CHANNEL_ID else interaction.channel
    )
    return channel or interaction.channel


# ================================================================
# SENDING HELPERS
# ================================================================
async def send_with_retry(channel, **kwargs):
    """discord.py already waits out normal rate limits; this adds a few retries for hiccups."""
    delays = [1, 2, 4, 8]
    for attempt in range(len(delays) + 1):
        try:
            return await channel.send(**kwargs)
        except discord.HTTPException as e:
            retryable = e.status == 429 or e.status >= 500
            if not retryable or attempt == len(delays):
                raise
            print(f"⏳ Send failed ({e.status}), retrying in {delays[attempt]}s")
            await asyncio.sleep(delays[attempt])


async def dm_member(user: discord.abc.User, guild: discord.Guild, title: str, reason: str, color: int, note: str = ""):
    """DMs the punished member. Returns the message (so it can be deleted again) or None."""
    if not DM_PUNISHED_MEMBERS or getattr(user, "bot", False):
        return None
    embed = discord.Embed(title=bold(title), color=color, timestamp=discord.utils.utcnow())
    embed.add_field(name=bold("Reason"), value=short(reason, 1000), inline=False)
    if note:
        embed.add_field(name=bold("Info"), value=short(note, 1000), inline=False)
    embed.set_footer(text=guild.name)
    try:
        return await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        return None


async def undo_dm(message):
    """Removes a DM again when the action it announced failed."""
    if message is None:
        return
    try:
        await message.delete()
    except discord.HTTPException:
        pass


# ================================================================
# PUNISHMENT DETAILS BUTTON
# ================================================================
class PunishmentDetailsButton(discord.ui.DynamicItem[discord.ui.Button], template=r"punishment_details_(?P<case>[0-9]+)"):
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
                bold("Details for this case aren't available anymore."), ephemeral=True
            )

        display_type = record.get("display_type", record["type"])
        style = TYPE_STYLE.get(display_type) or TYPE_STYLE.get(record["type"]) or ((150, 150, 150), 0)

        embed = discord.Embed(
            title=bold(f"Case ELT-{self.case_no:04d} — {display_type.title()}"),
            color=discord.Color.from_rgb(*style[0]),
        )
        embed.add_field(name=bold("User"), value=f"<@{record['user_id']}> ({bold(record['user_tag'])})", inline=False)
        embed.add_field(name=bold("Punisher"), value=f"<@{record['punisher_id']}> ({bold(record['punisher_tag'])})", inline=False)
        embed.add_field(name=bold("Reason"), value=bold(short(record["reason"], 300)), inline=False)
        embed.add_field(name=bold("Date"), value=bold(record["date_text"]), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ================================================================
# WARNING SYSTEM
# ================================================================
def _warning_is_active(record: dict) -> bool:
    return record.get("type") == "WARNING" and record.get("warning_active", True) is not False


def _parse_warning_time(record: dict):
    value = record.get("warning_issued_at")
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def active_warning_records(member_id: int):
    records = [
        (case_no, record)
        for case_no, record in punishment_records.items()
        if record.get("user_id") == member_id and _warning_is_active(record)
    ]
    records.sort(key=lambda item: (item[1].get("warning_issued_at", ""), item[0]))
    return records


def active_warning_count(member_id: int) -> int:
    return len(active_warning_records(member_id))


def warning_percent(count: int) -> int:
    return 0 if count <= 0 else 33 if count == 1 else 66 if count == 2 else 100


async def update_warning_roles(guild: discord.Guild, member: discord.Member, count: int):
    """1 warning: Warn 1 | 2 warnings: Warn 2 | 0 or 3+: none."""
    warn1 = guild.get_role(WARN_1_ROLE_ID)
    warn2 = guild.get_role(WARN_2_ROLE_ID)
    if warn1 is None or warn2 is None:
        print("⚠️ Warning role not found — check WARN_1_ROLE_ID / WARN_2_ROLE_ID")
        return

    wanted = {warn1} if count == 1 else {warn2} if count == 2 else set()
    current = {r for r in (warn1, warn2) if r in member.roles}
    add_roles, remove_roles = wanted - current, current - wanted

    try:
        if add_roles:
            await member.add_roles(*add_roles, reason="Warning level updated")
        if remove_roles:
            await member.remove_roles(*remove_roles, reason="Warning level updated")
    except discord.Forbidden:
        print(f"❌ Bot cannot manage warning roles for {member} — move the bot role above them.")
    except discord.HTTPException as e:
        print(f"❌ Failed to update warning roles for {member}: {e}")


# ================================================================
# WARNING CLEARED CARD
# ================================================================
async def post_warning_cleared_card(guild, member, punisher, reason, cleared_cases=None):
    """Posts the green WARNING CLEARED card to its channel."""
    cleared_cases = list(cleared_cases or [])
    case_no = next_case()
    date_text = discord.utils.utcnow().strftime("%d/%m/%Y")

    try:
        avatar_bytes = await member.display_avatar.replace(size=256, format="png").read()
    except Exception as e:
        print(f"⚠️ Couldn't fetch avatar for warning-clear card {member}: {e}")
        avatar_bytes = None

    user_name, punisher_name = card_name(member), card_name(punisher)
    max_bytes = min(MAX_BYTES, int(guild.filesize_limit * 0.9))

    try:
        card_bytes = await asyncio.to_thread(
            render_card_gif, user_name, punisher_name, reason, "WARNING CLEARED",
            case_no, date_text, avatar_bytes, max_bytes,
        )
        ext = "gif"
    except Exception as e:
        print(f"⚠️ Warning-clear GIF failed ({type(e).__name__}: {e}) — falling back to PNG")
        try:
            card_bytes = await asyncio.to_thread(
                render_card, user_name, punisher_name, reason, "WARNING CLEARED",
                case_no, date_text, avatar_bytes,
            )
            ext = "png"
        except Exception as e2:
            print(f"❌ Failed to render warning-clear card: {type(e2).__name__}: {e2}")
            return False

    punishment_records[case_no] = {
        "user_id": member.id,
        "user_tag": str(member),
        "punisher_id": punisher.id,
        "punisher_tag": str(punisher),
        "reason": reason,
        "type": "WARNING CLEARED",
        "display_type": "WARNING CLEARED",
        "date_text": date_text,
        "cleared_warning_cases": cleared_cases,
    }
    save_db()

    channel = guild.get_channel(WARNING_CLEAR_CHANNEL_ID)
    if channel is None:
        print(f"❌ Warning cleared channel {WARNING_CLEAR_CHANNEL_ID} was not found.")
        return False

    file = discord.File(io.BytesIO(card_bytes), filename=f"warning_cleared_{case_no:04d}.{ext}")
    view = discord.ui.View(timeout=None)
    view.add_item(PunishmentDetailsButton(case_no))

    try:
        await send_with_retry(channel, file=file, view=view)
        print(f"🟢 WARNING CLEARED case ELT-{case_no:04d} posted for {member}")
        return True
    except Exception as e:
        print(f"❌ Failed to send warning-clear card: {e}")
        return False


# ================================================================
# NORMAL PUNISHMENT CARD
# ================================================================
async def issue_punishment(
    interaction: discord.Interaction,
    member: discord.abc.User,
    ptype: str,
    reason: str,
    *,
    card_ptype: str = None,
    record_extra: dict = None,
    confirm_note: str = "",
):
    """
    Renders + posts the card and saves the case.
    Returns the case number on success, or None if it failed.
    card_ptype lets a 3rd warning DISPLAY as BAN while the record stays WARNING.
    """
    case_no = next_case()
    render_type = (card_ptype or ptype).upper()

    try:
        avatar_bytes = await member.display_avatar.replace(size=256, format="png").read()
    except Exception as e:
        print(f"⚠️ Couldn't fetch avatar for {member}: {e}")
        avatar_bytes = None

    date_text = discord.utils.utcnow().strftime("%d/%m/%Y")
    user_name, punisher_name = card_name(member), card_name(interaction.user)
    max_bytes = min(MAX_BYTES, int(interaction.guild.filesize_limit * 0.9))

    try:
        card_bytes = await asyncio.to_thread(
            render_card_gif, user_name, punisher_name, reason, render_type,
            case_no, date_text, avatar_bytes, max_bytes,
        )
        ext = "gif"
    except Exception as e:
        print(f"⚠️ Animated card failed ({type(e).__name__}: {e}) — falling back to the still card")
        try:
            card_bytes = await asyncio.to_thread(
                render_card, user_name, punisher_name, reason, render_type,
                case_no, date_text, avatar_bytes,
            )
            ext = "png"
        except Exception as e2:
            print(f"❌ Failed to render punishment card: {type(e2).__name__}: {e2}")
            await interaction.followup.send(
                f"⚠️ {bold('Something went wrong generating the punishment card.')}", ephemeral=True
            )
            return None

    punishment_records[case_no] = {
        "user_id": member.id,
        "user_tag": str(member),
        "punisher_id": interaction.user.id,
        "punisher_tag": str(interaction.user),
        "reason": reason,
        "type": ptype,
        "display_type": render_type,
        "date_text": date_text,
    }
    if record_extra:
        punishment_records[case_no].update(record_extra)
    save_db()

    log_channel = get_log_channel(interaction)
    file = discord.File(io.BytesIO(card_bytes), filename=f"punishment_case_{case_no:04d}.{ext}")
    view = discord.ui.View(timeout=None)
    view.add_item(PunishmentDetailsButton(case_no))

    try:
        await send_with_retry(log_channel, file=file, view=view)
        print(f"✅ Case ELT-{case_no:04d} ({ptype}/{render_type}) posted for {member} by {interaction.user}")
    except Exception as e:
        print(f"❌ Failed to send punishment card: {e}")
        await interaction.followup.send(
            f"⚠️ {bold('The case was saved but I could not post the card — check my permissions in that channel.')}",
            ephemeral=True,
        )
        return case_no   # the punishment itself IS recorded

    text = f"✅ {bold(f'{ptype.title()} logged for')} {member.mention} {bold('in')} {log_channel.mention}."
    if confirm_note:
        text += f" {confirm_note}"
    await interaction.followup.send(text, ephemeral=True)
    return case_no


async def reject(interaction: discord.Interaction, command: str, target: discord.abc.User, msg: str):
    print(f"🚫 /{command}: {interaction.user} -> {target} blocked: {msg}")
    await interaction.followup.send(f"⚠️ {bold(msg)}", ephemeral=True)


# ================================================================
# WARNING EXPIRATION
# ================================================================
async def expire_warnings():
    """No new warning for WARNING_EXPIRE_DAYS -> the whole active streak is cleared."""
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    now = datetime.now(timezone.utc)
    by_member: dict[int, list] = {}
    for case_no, record in punishment_records.items():
        if _warning_is_active(record):
            by_member.setdefault(record["user_id"], []).append((case_no, record))

    changed = False

    for member_id, records in by_member.items():
        latest = max(
            records,
            key=lambda item: (_parse_warning_time(item[1]) or datetime.min.replace(tzinfo=timezone.utc), item[0]),
        )
        latest_time = _parse_warning_time(latest[1])

        # Old records have no exact timestamp — don't guess their expiry.
        if latest_time is None or now - latest_time < timedelta(days=WARNING_EXPIRE_DAYS):
            continue

        cleared_cases = []
        for case_no, record in records:
            record["warning_active"] = False
            record["warning_cleared_at"] = now.isoformat()
            record["warning_clear_method"] = "automatic_30_day_expiry"
            cleared_cases.append(case_no)
        changed = True
        save_db()   # saved BEFORE posting, so a crash can never post the same clear twice

        member = guild.get_member(member_id)
        if member is None:
            continue   # left the server: cleared quietly, nothing to announce

        try:
            await update_warning_roles(guild, member, 0)
            reason = (
                f"No new warning was received for {WARNING_EXPIRE_DAYS} days. "
                f"All active warnings were automatically cleared."
            )
            punisher = guild.me or bot.user
            if punisher:
                await post_warning_cleared_card(guild, member, punisher, reason, cleared_cases)
        except Exception:
            print(f"❌ Expiry follow-up failed for {member_id}:")
            traceback.print_exc()

    if changed:
        save_db()


@tasks.loop(hours=1)
async def warning_expiry_loop():
    try:
        await expire_warnings()
    except Exception:
        print("❌ warning expiry run failed (it will retry next hour):")
        traceback.print_exc()


@warning_expiry_loop.before_loop
async def _wait_ready_for_warning_expiry():
    await bot.wait_until_ready()


# ================================================================
# SLASH COMMAND SYNC
# ================================================================
async def sync_commands() -> bool:
    for attempt in range(1, 6):
        try:
            synced = await bot.tree.sync(guild=GUILD)
            print(f"🔄 Synced {len(synced)} slash command(s) to guild {GUILD_ID}")
            return True
        except Exception as e:
            print(f"❌ Sync failed (attempt {attempt}/5): {e}")
            await asyncio.sleep(5 * attempt)
    return False


@tasks.loop(hours=6)
async def resync_loop():
    try:
        await sync_commands()
    except Exception:
        traceback.print_exc()


@resync_loop.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


@bot.event
async def setup_hook():
    bot.add_dynamic_items(PunishmentDetailsButton)
    resync_loop.start()
    warning_expiry_loop.start()


@bot.command(name="sync")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def sync_cmd(ctx: commands.Context):
    ok = await sync_commands()
    await ctx.reply("✅ Slash commands re-synced." if ok else "❌ Sync failed, check the logs.")


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"👮 Staff role IDs: {sorted(STAFF_ROLE_IDS) or 'none set'}")
    print(f"🛡️ Moderator role ID: {MOD_ROLE_ID}")
    print(f"🔨 Who can ban: {TIER_LABEL[BAN_MIN_TIER]} or higher")
    print(f"⚠️ Warning expiration: {WARNING_EXPIRE_DAYS} days")
    print(f"🟢 Warning cleared channel: {WARNING_CLEAR_CHANNEL_ID}")

    guild = bot.get_guild(GUILD_ID)
    if guild is not None:
        if guild.get_role(WARN_1_ROLE_ID) is None or guild.get_role(WARN_2_ROLE_ID) is None:
            print("⚠️ Warn 1 / Warn 2 role not found — check the role IDs")
        if guild.get_channel(WARNING_CLEAR_CHANNEL_ID) is None:
            print("⚠️ WARNING_CLEAR_CHANNEL_ID channel not found")


@bot.event
async def on_member_join(member: discord.Member):
    """Leaving and rejoining doesn't wash warnings away: the Warn role comes back."""
    if member.guild.id != GUILD_ID:
        return
    count = active_warning_count(member.id)
    if count:
        await update_warning_roles(member.guild, member, min(count, MAX_WARNINGS - 1))


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
        return
    print(f"❌ Prefix command error: {error}")


# ================================================================
# /WARN
# ================================================================
@bot.tree.command(
    name="warn",
    description="Give a warning to a member. Warnings expire after 30 days without a new warning.",
    guild=GUILD,
)
@app_commands.describe(member="The member being warned", reason="Why they're being warned")
@app_commands.guild_only()
@require_tier("staff")
async def warn_cmd(interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 300]):
    await interaction.response.defer(ephemeral=True)

    problem = target_problem(interaction, member, needs_bot_rank=False)
    if problem:
        return await reject(interaction, "warn", member, problem)

    guild = interaction.guild

    # One warn at a time: counting, the card and the roles can't be raced by a second staff member.
    async with warning_lock:
        warning_count = active_warning_count(member.id) + 1
        percent = warning_percent(warning_count)
        auto_ban = warning_count >= MAX_WARNINGS and can_ban(interaction.user)

        card_type = "BAN" if auto_ban else "WARNING"
        card_reason = reason
        if auto_ban:
            card_reason = f"{reason} — {MAX_WARNINGS} active warnings reached. Automatic ban applied."[:300]

        record_extra = {
            "warning_active": True,
            "warning_issued_at": discord.utils.utcnow().isoformat(),
            "warning_count": warning_count,
            "warning_percent": percent,
        }
        if auto_ban:
            record_extra["automatic_ban"] = True

        # The card renderer reads the severity from TYPE_STYLE["WARNING"]: set it for this card only.
        original_style = TYPE_STYLE["WARNING"]
        TYPE_STYLE["WARNING"] = (original_style[0], percent)
        try:
            case_no = await issue_punishment(
                interaction, member, "WARNING", card_reason,
                card_ptype=card_type,
                record_extra=record_extra,
                confirm_note="" if auto_ban else f"{bold('Active warnings:')} **{warning_count}/{MAX_WARNINGS}**.",
            )
        finally:
            TYPE_STYLE["WARNING"] = original_style

        if case_no is None:
            # Card failed before anything was saved: no record, so don't touch the roles.
            return

        await update_warning_roles(guild, member, warning_count if auto_ban else min(warning_count, MAX_WARNINGS - 1))

    # ---- 3rd warning by someone allowed to ban: automatic ban ----
    if auto_ban:
        dm = await dm_member(
            member, guild, f"You have been banned from {guild.name}", reason, 0xE53935,
            f"You reached {MAX_WARNINGS} active warnings.",
        )
        try:
            await member.ban(reason=audit_reason(
                f"{MAX_WARNINGS} active warnings reached: {reason}", "automatically banned", bot.user
            ))
            print(f"🔨 Automatic ban applied to {member} after warning #{warning_count}")
        except discord.Forbidden:
            await undo_dm(dm)
            print(f"❌ Automatic ban failed for {member}: bot lacks ban permission/hierarchy.")
            await interaction.followup.send(
                "⚠️ The BAN card was posted, but Discord refused the automatic ban. "
                "Check my Ban Members permission and role position.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await undo_dm(dm)
            print(f"❌ Automatic ban HTTPException for {member}: {e}")
            await interaction.followup.send(f"⚠️ The BAN card was posted, but the automatic ban failed: {e}", ephemeral=True)
        return

    # ---- normal warning: tell the member ----
    await dm_member(
        member, guild, f"You received a warning in {guild.name}", reason, 0xF5A623,
        f"Active warnings: {warning_count}/{MAX_WARNINGS}. "
        f"They clear after {WARNING_EXPIRE_DAYS} days without a new warning.",
    )

    # ---- 3rd+ warning from Staff / a Moderator: no ban, ping the Administrators ----
    if warning_count >= MAX_WARNINGS:
        admin_role = guild.get_role(ADMIN_ROLE_ID)
        who = admin_role.mention if admin_role else "Administrators"
        try:
            await get_log_channel(interaction).send(
                f"{who} {member.mention} now has **{warning_count}** active warnings. "
                f"An Administrator needs to decide on a ban.",
                allowed_mentions=discord.AllowedMentions(
                    roles=[admin_role] if admin_role else False, users=False, everyone=False
                ),
            )
        except discord.HTTPException as e:
            print(f"⚠️ Couldn't post the 3rd-warning notice: {e}")

        await interaction.followup.send(
            f"ℹ️ {member.mention} now has **{warning_count}** active warnings. "
            f"You can't ban, so I pinged the Administrators.",
            ephemeral=True,
        )


# ================================================================
# /UNWARN
# ================================================================
@bot.tree.command(name="unwarn", description="Remove the most recent active warning from a member.", guild=GUILD)
@app_commands.describe(member="The member whose latest warning should be removed", reason="Why the warning is being removed")
@app_commands.guild_only()
@require_tier("mod")
async def unwarn_cmd(interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 300]):
    await interaction.response.defer(ephemeral=True)

    problem = target_problem(interaction, member, needs_bot_rank=False)
    if problem:
        return await reject(interaction, "unwarn", member, problem)

    async with warning_lock:
        active = active_warning_records(member.id)
        if not active:
            return await interaction.followup.send(f"ℹ️ {member.mention} has no active warnings.", ephemeral=True)

        case_no, record = active[-1]   # newest only
        record["warning_active"] = False
        record["warning_cleared_at"] = discord.utils.utcnow().isoformat()
        record["warning_cleared_by_id"] = interaction.user.id
        record["warning_cleared_by_tag"] = str(interaction.user)
        record["warning_clear_method"] = "manual_unwarn"
        record["warning_clear_reason"] = reason
        save_db()

        remaining = active_warning_count(member.id)
        await update_warning_roles(interaction.guild, member, min(remaining, MAX_WARNINGS - 1))

    clear_reason = (
        f"Warning ELT-{case_no:04d} was removed by {card_name(interaction.user)}. Reason: {reason}"
    )[:300]
    await post_warning_cleared_card(interaction.guild, member, interaction.user, clear_reason, [case_no])

    await interaction.followup.send(
        f"🟢 Warning ELT-{case_no:04d} removed from {member.mention}. Active warnings remaining: **{remaining}**.",
        ephemeral=True,
    )


# ================================================================
# /MUTE and /TIMEOUT
# ================================================================
async def _apply_timeout(interaction, member, duration, reason, command, ptype, shown_reason):
    """Shared by /mute and /timeout."""
    try:
        await member.timeout(duration, reason=audit_reason(reason, ptype.lower(), interaction.user))
    except discord.Forbidden:
        return await reject(interaction, command, member, "I do not have permission to time out that member.")
    except discord.HTTPException as e:
        print(f"❌ /{command} HTTPException for {member}: {e}")
        return await interaction.followup.send(f"⚠️ {bold('Failed to apply the timeout:')} {e}", ephemeral=True)

    await dm_member(
        member, interaction.guild, f"You were {ptype.lower()}d in {interaction.guild.name}"
        if ptype == "MUTE" else f"You were timed out in {interaction.guild.name}",
        reason, 0x8E6CFF, shown_reason,
    )
    await issue_punishment(interaction, member, ptype, f"{reason} ({shown_reason})")


@bot.tree.command(name="mute", description="Time a member out and log a mute card.", guild=GUILD)
@app_commands.describe(member="The member being muted", minutes="How long to mute them for, in minutes", reason="Why they're being muted")
@app_commands.guild_only()
@require_tier("mod")
async def mute_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: app_commands.Range[str, 1, 300],
):
    await interaction.response.defer(ephemeral=True)
    problem = target_problem(interaction, member)
    if problem:
        return await reject(interaction, "mute", member, problem)
    await _apply_timeout(interaction, member, timedelta(minutes=minutes), reason, "mute", "MUTE", f"for {minutes}m")


@bot.tree.command(name="timeout", description="Time a member out for minutes, hours or days and log a timeout card.", guild=GUILD)
@app_commands.describe(member="The member being timed out", amount="How long (a number)", unit="Minutes, hours or days", reason="Why they're being timed out")
@app_commands.choices(unit=[
    app_commands.Choice(name="Minutes", value="minutes"),
    app_commands.Choice(name="Hours", value="hours"),
    app_commands.Choice(name="Days", value="days"),
])
@app_commands.guild_only()
@require_tier("mod")
async def timeout_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: app_commands.Range[int, 1, 40320],
    unit: app_commands.Choice[str],
    reason: app_commands.Range[str, 1, 300],
):
    await interaction.response.defer(ephemeral=True)

    try:
        duration = timedelta(**{unit.value: amount})
    except OverflowError:
        duration = timedelta(days=9999)

    if duration > timedelta(days=28):
        return await reject(interaction, "timeout", member, "Discord only allows timeouts up to 28 days.")

    problem = target_problem(interaction, member)
    if problem:
        return await reject(interaction, "timeout", member, problem)

    unit_label = unit.value if amount != 1 else unit.value[:-1]
    await _apply_timeout(interaction, member, duration, reason, "timeout", "TIMEOUT", f"for {amount} {unit_label}")


# ================================================================
# /KICK
# ================================================================
@bot.tree.command(name="kick", description="Kick a member and log a kick card.", guild=GUILD)
@app_commands.describe(member="The member being kicked", reason="Why they're being kicked")
@app_commands.guild_only()
@require_tier("mod")
async def kick_cmd(interaction: discord.Interaction, member: discord.Member, reason: app_commands.Range[str, 1, 300]):
    await interaction.response.defer(ephemeral=True)

    problem = target_problem(interaction, member)
    if problem:
        return await reject(interaction, "kick", member, problem)

    # DM first: after the kick the bot no longer shares a server with them.
    dm = await dm_member(member, interaction.guild, f"You were kicked from {interaction.guild.name}", reason, 0xFF7043)

    try:
        await member.kick(reason=audit_reason(reason, "kicked", interaction.user))
    except discord.Forbidden:
        await undo_dm(dm)
        return await reject(interaction, "kick", member, "I do not have permission to kick that member.")
    except discord.HTTPException as e:
        await undo_dm(dm)
        print(f"❌ /kick HTTPException for {member}: {e}")
        return await interaction.followup.send(f"⚠️ {bold('Failed to kick:')} {e}", ephemeral=True)

    await issue_punishment(interaction, member, "KICK", reason)


# ================================================================
# /BAN  (also works on someone who already left the server)
# ================================================================
@bot.tree.command(name="ban", description="Ban a member and log a ban card.", guild=GUILD)
@app_commands.describe(member="Who to ban (can be someone who already left: paste their user ID)", reason="Why they're being banned")
@app_commands.guild_only()
@require_tier(BAN_MIN_TIER)
async def ban_cmd(interaction: discord.Interaction, member: discord.User, reason: app_commands.Range[str, 1, 300]):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    in_server = guild.get_member(member.id)

    if in_server is not None:
        problem = target_problem(interaction, in_server)
    elif member.id == interaction.user.id:
        problem = "You can't punish yourself."
    elif bot.user and member.id == bot.user.id:
        problem = "I can't punish myself."
    elif member.id == guild.owner_id:
        problem = "You can't punish the server owner."
    else:
        problem = None

    if problem:
        return await reject(interaction, "ban", member, problem)

    dm = await dm_member(member, guild, f"You were banned from {guild.name}", reason, 0xE53935) if in_server else None

    try:
        await guild.ban(member, reason=audit_reason(reason, "banned", interaction.user))
    except discord.Forbidden:
        await undo_dm(dm)
        return await reject(interaction, "ban", member, "I do not have permission to ban that member.")
    except discord.HTTPException as e:
        await undo_dm(dm)
        print(f"❌ /ban HTTPException for {member}: {e}")
        return await interaction.followup.send(f"⚠️ {bold('Failed to ban:')} {e}", ephemeral=True)

    await issue_punishment(interaction, member, "BAN", reason)


# ================================================================
# /WARNINGS and /HISTORY  (read-only)
# ================================================================
@bot.tree.command(name="warnings", description="Show a member's active warnings and when they clear.", guild=GUILD)
@app_commands.describe(member="The member to check")
@app_commands.guild_only()
@require_tier("staff")
async def warnings_cmd(interaction: discord.Interaction, member: discord.User):
    await interaction.response.defer(ephemeral=True)

    active = active_warning_records(member.id)
    if not active:
        return await interaction.followup.send(f"ℹ️ {member.mention} has no active warnings.", ephemeral=True)

    lines = [
        f"**ELT-{case_no:04d}** • {rec['date_text']} • by <@{rec['punisher_id']}>\n{short(rec['reason'], 150)}"
        for case_no, rec in active
    ]
    embed = discord.Embed(
        title=bold(f"Active warnings — {card_name(member)}"),
        description="\n\n".join(lines)[:4000],
        color=discord.Color.orange(),
    )

    times = [t for t in (_parse_warning_time(rec) for _, rec in active) if t]
    if times:
        clears = max(times) + timedelta(days=WARNING_EXPIRE_DAYS)
        embed.add_field(name=bold("Auto-clear"), value=discord.utils.format_dt(clears, "R"), inline=False)
    embed.set_footer(text=f"{len(active)}/{MAX_WARNINGS} active warnings")
    embed.set_thumbnail(url=member.display_avatar.url)

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="history", description="Show a member's last punishment cases.", guild=GUILD)
@app_commands.describe(member="The member to check (works for people who left: paste their user ID)")
@app_commands.guild_only()
@require_tier("mod")
async def history_cmd(interaction: discord.Interaction, member: discord.User):
    await interaction.response.defer(ephemeral=True)

    cases = sorted(
        ((c, r) for c, r in punishment_records.items() if r.get("user_id") == member.id),
        key=lambda item: item[0],
        reverse=True,
    )
    if not cases:
        return await interaction.followup.send(f"ℹ️ No cases found for {member.mention}.", ephemeral=True)

    lines = [
        f"`ELT-{c:04d}` **{r.get('display_type', r['type']).title()}** • {r['date_text']}\n{short(r['reason'], 100)}"
        for c, r in cases[:15]
    ]
    embed = discord.Embed(
        title=bold(f"Case history — {card_name(member)}"),
        description="\n\n".join(lines)[:4000],
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Showing {min(len(cases), 15)} of {len(cases)} cases")
    embed.set_thumbnail(url=member.display_avatar.url)

    await interaction.followup.send(embed=embed, ephemeral=True)


# ================================================================
# APP COMMAND ERROR
# ================================================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    command_name = interaction.command.name if interaction.command else "unknown"

    # A "you are not allowed" rejection is normal: one short line, no traceback.
    if isinstance(error, (NotAllowed, app_commands.MissingPermissions)):
        print(f"🚫 /{command_name}: {interaction.user} not allowed")
    else:
        print("\n" + "=" * 70)
        print(f"❌ APP COMMAND ERROR  /{command_name}  by {interaction.user} ({interaction.user.id})")
        print(f"Error: {type(error).__name__}: {error}")
        original = getattr(error, "original", None)
        if original is not None:
            print(f"Original: {type(original).__name__}: {original}")
        traceback.print_exception(type(error), error, error.__traceback__)
        print("=" * 70 + "\n")

    if isinstance(error, app_commands.MissingPermissions):
        msg = f"⚠️ {bold('You do not have permission to do that.')}"
    elif isinstance(error, NotAllowed):
        msg = f"⚠️ {bold(str(error))}"
    elif isinstance(error, app_commands.TransformerError):
        msg = f"⚠️ {bold('I could not find that member in the server — they may have left.')}"
    elif isinstance(error, app_commands.CheckFailure):
        msg = f"⚠️ {bold('You are not allowed to use this command.')}"
    else:
        msg = f"⚠️ {bold('Something went wrong running that command.')}"

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# ================================================================
# START BOT
# ================================================================
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Add it in Railway's Variables tab, then redeploy.")

bot.run(TOKEN)
