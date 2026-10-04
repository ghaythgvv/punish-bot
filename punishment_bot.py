"""
ELT Punishment Bot
=======================================
Slash commands (/warn, /unwarn, /mute, /timeout, /kick, /ban) so staff get Discord's own
UI — a member picker, typed fields, and built-in validation — instead of typing
raw text commands. Wraps punishment_gif.py's render_card_gif() to post an
ANIMATED punishment card with the target's own avatar and display name, plus a
"View Punishment Details" button underneath. If the animated card can't be made
for any reason, it falls back to the still PNG card from punishment_card.py.

Who can use what:
    Staff (lowest role in STAFF_ROLE_IDS, or above) -> /warn ONLY
    Moderators (MOD_ROLE_ID, or above)              -> /warn /unwarn /mute /timeout /kick
    Administrators + server owner                   -> everything, including /ban

Nobody can punish: the server owner, an Administrator, or anyone whose top role is ABOVE the
Moderator role. And nobody can punish someone whose top role is equal to or above their own.

Bans: only Administrators can /ban and only they trigger the automatic ban on the 3rd warning
(set MODS_CAN_BAN = True below to let Moderators do it too). When a Staff member or Moderator
gives a 3rd warning, no ban happens: the warning is posted and the Administrators are pinged.

/ban also works on someone who ALREADY LEFT the server: paste their user ID in the member box.

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
    - IMPORTANT (case numbers): in Railway add a Volume to this service and mount
      it at /data. Case numbers and details are saved there, so they survive
      restarts and redeploys. Without a volume the file is wiped on every deploy.
    - This bot needs its OWN Discord application + token. If another bot shares
      the token, the two overwrite each other's slash commands.

If the slash commands ever disappear, an admin can mention the bot and type
"sync" (for example: @ELT Punishment sync) to bring them back instantly.
"""

import io
import os
import re
import json
import asyncio
import traceback
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from punishment_card import render_card, TYPE_STYLE
from punishment_gif import render_card_gif, clean_for_card, MAX_BYTES

# The card renderer reads this shared dictionary at runtime.
# This gives the automatic/manual warning-clear card its own green style.
TYPE_STYLE.setdefault("WARNING CLEARED", ((70, 220, 120), 100))

warning_card_style_lock = asyncio.Lock()

# =========================== CONFIG ===========================
TOKEN = os.environ.get("DISCORD_TOKEN")

GUILD_ID = 1410440666747633707
GUILD = discord.Object(id=GUILD_ID)

PUNISHMENT_LOG_CHANNEL_ID = None

# Moderator role.
MOD_ROLE_ID = 1513904125086011402

# Administrator role (pinged when a 3rd warning needs a ban decision).
ADMIN_ROLE_ID = 1513904120803889243

# False = only Administrators/owner can /ban and trigger the automatic 3rd-warning ban.
# True  = Moderators can too.
MODS_CAN_BAN = False

# Warning roles / warning system
WARN_1_ROLE_ID = 1513904153900875897
WARN_2_ROLE_ID = 1513904154719027291

WARNING_EXPIRE_DAYS = 30

# Green WARNING CLEARED cards are posted here.
WARNING_CLEAR_CHANNEL_ID = 1540154905644367893

# Staff role IDs (they can only use /warn).
STAFF_ROLE_IDS = {
    int(x)
    for x in os.environ.get(
        "STAFF_ROLE_IDS",
        "1513904136783925380"
    ).replace(" ", "").split(",")
    if x.isdigit()
}

# Where case numbers + details are saved.
DATA_DIR = (
    os.environ.get("DATA_DIR")
    or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or os.path.dirname(os.path.abspath(__file__))
)

DB_PATH = os.path.join(DATA_DIR, "punishments.json")
# ================================================================


intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned,
    intents=intents
)


_BOLD_UPPER_START = 0x1D5D4
_BOLD_LOWER_START = 0x1D5EE
_BOLD_DIGIT_START = 0x1D7EC


def bold(text: str) -> str:
    """Converts A-Z, a-z, 0-9 to Mathematical Sans-Serif Bold."""
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


# ================================================================
# CASE NUMBERS + DATABASE
# ================================================================

def load_db():
    try:
        with open(DB_PATH, encoding="utf-8") as f:
            data = json.load(f)

        return (
            int(data.get("counter", 0)),
            {int(k): v for k, v in data.get("records", {}).items()},
        )

    except FileNotFoundError:
        return 0, {}

    except Exception as e:
        print(f"⚠️ Couldn't read {DB_PATH}: {e}")
        return 0, {}


def save_db():
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

        tmp = DB_PATH + ".tmp"

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"counter": case_counter, "records": punishment_records},
                f,
                ensure_ascii=False
            )

        os.replace(tmp, DB_PATH)

    except Exception as e:
        print(f"❌ Couldn't save {DB_PATH}: {e}")


case_counter, punishment_records = load_db()

print(
    f"📁 Case data: {DB_PATH} "
    f"(last case #{case_counter:04d}, "
    f"{len(punishment_records)} saved)"
)


def card_name(member: discord.abc.User) -> str:
    """Name shown on the card."""
    return clean_for_card(member.display_name) or member.name


def audit_reason(reason: str, action: str, by: discord.abc.User) -> str:
    """Reason text for Discord's audit log."""
    return f"{reason} — {action} by {by}"[:512]


# ================================================================
# PERMISSIONS
# ================================================================
# Four levels, worked out from the member's roles:
#   admin -> server owner, or anyone with the Administrator permission
#   mod   -> Moderator role (MOD_ROLE_ID) or any role above it
#   staff -> lowest role in STAFF_ROLE_IDS, or any role above it
#   none  -> everyone else

TIER_RANK = {"none": 0, "staff": 1, "mod": 2, "admin": 3}
TIER_LABEL = {"staff": "Staff", "mod": "Moderators", "admin": "Administrators"}

# Lowest level allowed to /ban and to trigger the automatic 3rd-warning ban.
BAN_MIN_TIER = "mod" if MODS_CAN_BAN else "admin"


class NotAllowed(app_commands.CheckFailure):
    """Raised by require_tier. The text is shown to the user."""


def actor_tier(member: discord.Member) -> str:
    guild = member.guild

    if member.id == guild.owner_id or member.guild_permissions.administrator:
        return "admin"

    mod_role = guild.get_role(MOD_ROLE_ID)

    if mod_role and member.top_role >= mod_role:
        return "mod"

    staff_roles = [
        r for r in (guild.get_role(i) for i in STAFF_ROLE_IDS) if r
    ]

    if staff_roles and member.top_role >= min(staff_roles):
        return "staff"

    return "none"


def can_ban(member: discord.Member) -> bool:
    return TIER_RANK[actor_tier(member)] >= TIER_RANK[BAN_MIN_TIER]


def require_tier(min_tier: str):
    """Only members of this level (or higher) can use the command."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if TIER_RANK[actor_tier(interaction.user)] >= TIER_RANK[min_tier]:
            return True

        raise NotAllowed(
            f"This command is for {TIER_LABEL[min_tier]} or higher."
        )

    return app_commands.check(predicate)


def target_problem(
    interaction: discord.Interaction,
    member: discord.Member,
    needs_bot_rank: bool = True
):
    """Returns a message if the punishment shouldn't go ahead."""

    guild = interaction.guild

    if member.id == interaction.user.id:
        return "You can't punish yourself."

    if bot.user and member.id == bot.user.id:
        return "I can't punish myself."

    # --- protected members: nobody can touch them through the bot ---
    if member.id == guild.owner_id:
        return "You can't punish the server owner."

    if member.guild_permissions.administrator:
        return "You can't punish an administrator."

    mod_role = guild.get_role(MOD_ROLE_ID)

    if mod_role and member.top_role > mod_role:
        return "That member is protected (their role is above Moderator)."

    # --- rank rules ---
    if (
        interaction.user.id != guild.owner_id
        and member.top_role >= interaction.user.top_role
    ):
        return "That member's highest role is equal to or above yours."

    if (
        needs_bot_rank
        and guild.me
        and member.top_role >= guild.me.top_role
    ):
        return (
            "That member's highest role is equal to "
            "or above mine — move my role higher."
        )

    return None


def get_log_channel(interaction: discord.Interaction):
    """Where the punishment cards are posted."""

    channel = (
        interaction.guild.get_channel(PUNISHMENT_LOG_CHANNEL_ID)
        if PUNISHMENT_LOG_CHANNEL_ID
        else interaction.channel
    )

    return channel or interaction.channel


# ================================================================
# SEND WITH RETRY
# ================================================================

async def send_with_retry(channel, **kwargs):
    """Send a message with retry logic."""

    max_attempts = 5
    backoff_delays = [1, 2, 4, 8, 16]

    for attempt in range(max_attempts):
        try:
            return await channel.send(**kwargs)

        except discord.errors.HTTPException as e:
            if e.status == 429:
                if attempt < max_attempts - 1:
                    delay = backoff_delays[attempt]
                    print(
                        f"⏳ Rate limited, retrying in {delay}s "
                        f"(attempt {attempt + 1}/{max_attempts})"
                    )
                    await asyncio.sleep(delay)
                else:
                    print(f"❌ Failed to send message after {max_attempts} attempts")
                    raise
            else:
                raise


# ================================================================
# PUNISHMENT DETAILS BUTTON
# ================================================================

class PunishmentDetailsButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"punishment_details_(?P<case>[0-9]+)",
):
    """View Punishment Details button."""

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
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match
    ):
        return cls(int(match["case"]))

    async def callback(self, interaction: discord.Interaction):
        record = punishment_records.get(self.case_no)

        if record is None:
            return await interaction.response.send_message(
                bold("Details for this case aren't available anymore."),
                ephemeral=True,
            )

        display_type = record.get("display_type", record["type"])

        style = (
            TYPE_STYLE.get(display_type)
            or TYPE_STYLE.get(record["type"])
            or ((150, 150, 150), 0)
        )
        color_rgb = style[0]

        embed = discord.Embed(
            title=bold(f"Case ELT-{self.case_no:04d} — {display_type.title()}"),
            color=discord.Color.from_rgb(*color_rgb),
        )

        embed.add_field(
            name=bold("User"),
            value=f"<@{record['user_id']}> ({bold(record['user_tag'])})",
            inline=False,
        )

        embed.add_field(
            name=bold("Punisher"),
            value=f"<@{record['punisher_id']}> ({bold(record['punisher_tag'])})",
            inline=False,
        )

        embed.add_field(
            name=bold("Reason"),
            value=bold(record["reason"]),
            inline=False,
        )

        embed.add_field(
            name=bold("Date"),
            value=bold(record["date_text"]),
            inline=True,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)


# ================================================================
# WARNING SYSTEM
# ================================================================

def _warning_is_active(record: dict) -> bool:
    """
    New warnings have warning_active=True.
    Old WARNING records without the field are treated as active.
    """

    return (
        record.get("type") == "WARNING"
        and record.get("warning_active", True) is not False
    )


def active_warning_records(member_id: int):
    records = [
        (case_no, record)
        for case_no, record in punishment_records.items()
        if record.get("user_id") == member_id and _warning_is_active(record)
    ]

    records.sort(
        key=lambda item: (item[1].get("warning_issued_at", ""), item[0])
    )

    return records


def active_warning_count(member_id: int) -> int:
    return len(active_warning_records(member_id))


def warning_percent(count: int) -> int:
    if count <= 0:
        return 0

    if count == 1:
        return 33

    if count == 2:
        return 66

    return 100


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


async def update_warning_roles(
    guild: discord.Guild,
    member: discord.Member,
    count: int
):
    """
    1 warning:  Warn 1 role
    2 warnings: Warn 2 role (remove Warn 1)
    0 or 3+:    remove both
    """

    warn1 = guild.get_role(WARN_1_ROLE_ID)
    warn2 = guild.get_role(WARN_2_ROLE_ID)

    if warn1 is None or warn2 is None:
        print("⚠️ Warning role not found — check WARN_1_ROLE_ID / WARN_2_ROLE_ID")
        return

    wanted = set()

    if count == 1:
        wanted.add(warn1)
    elif count == 2:
        wanted.add(warn2)

    current = {role for role in (warn1, warn2) if role in member.roles}

    add_roles = wanted - current
    remove_roles = current - wanted

    try:
        if add_roles:
            await member.add_roles(*add_roles, reason="Warning level updated")

        if remove_roles:
            await member.remove_roles(*remove_roles, reason="Warning level updated")

    except discord.Forbidden:
        print(
            f"❌ Bot cannot manage warning roles for {member} "
            f"— move the bot role above them."
        )

    except discord.HTTPException as e:
        print(f"❌ Failed to update warning roles for {member}: {e}")


# ================================================================
# WARNING CLEARED CARD
# ================================================================

async def post_warning_cleared_card(
    guild: discord.Guild,
    member: discord.Member,
    punisher: discord.abc.User,
    reason: str,
    cleared_cases=None,
):
    """Posts the green WARNING CLEARED card to the dedicated channel."""

    global case_counter

    cleared_cases = list(cleared_cases or [])

    case_counter += 1
    case_no = case_counter

    date_text = discord.utils.utcnow().strftime("%d/%m/%Y")

    try:
        avatar_bytes = await member.display_avatar.replace(
            size=256, format="png"
        ).read()

    except Exception as e:
        print(f"⚠️ Couldn't fetch avatar for warning-clear card {member}: {e}")
        avatar_bytes = None

    user_name = card_name(member)
    punisher_name = card_name(punisher)

    max_bytes = min(MAX_BYTES, int(guild.filesize_limit * 0.9))

    try:
        card_bytes = await asyncio.to_thread(
            render_card_gif,
            user_name,
            punisher_name,
            reason,
            "WARNING CLEARED",
            case_no,
            date_text,
            avatar_bytes,
            max_bytes,
        )

        ext = "gif"

    except Exception as e:
        print(
            f"⚠️ Warning-clear GIF failed "
            f"({type(e).__name__}: {e}) — falling back to PNG"
        )

        try:
            card_bytes = await asyncio.to_thread(
                render_card,
                user_name,
                punisher_name,
                reason,
                "WARNING CLEARED",
                case_no,
                date_text,
                avatar_bytes,
            )

            ext = "png"

        except Exception as e2:
            print(
                f"❌ Failed to render warning-clear card: "
                f"{type(e2).__name__}: {e2}"
            )

            save_db()
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

    file = discord.File(
        io.BytesIO(card_bytes),
        filename=f"warning_cleared_{case_no:04d}.{ext}",
    )

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
):
    """
    Render/post a punishment card and save its case record.

    card_ptype allows the third warning to DISPLAY as BAN
    while the database record remains WARNING.
    """

    global case_counter

    case_counter += 1
    case_no = case_counter

    save_db()

    render_type = (card_ptype or ptype).upper()

    try:
        avatar_bytes = await member.display_avatar.replace(
            size=256, format="png"
        ).read()

    except Exception as e:
        print(f"⚠️ Couldn't fetch avatar for {member}: {e}")
        avatar_bytes = None

    date_text = discord.utils.utcnow().strftime("%d/%m/%Y")

    user_name = card_name(member)
    punisher_name = card_name(interaction.user)

    max_bytes = min(MAX_BYTES, int(interaction.guild.filesize_limit * 0.9))

    try:
        card_bytes = await asyncio.to_thread(
            render_card_gif,
            user_name,
            punisher_name,
            reason,
            render_type,
            case_no,
            date_text,
            avatar_bytes,
            max_bytes,
        )

        ext = "gif"

    except Exception as e:
        print(
            f"⚠️ Animated card failed ({type(e).__name__}: {e}) "
            f"— falling back to the still card"
        )

        try:
            card_bytes = await asyncio.to_thread(
                render_card,
                user_name,
                punisher_name,
                reason,
                render_type,
                case_no,
                date_text,
                avatar_bytes,
            )

            ext = "png"

        except Exception as e2:
            print(f"❌ Failed to render punishment card: {type(e2).__name__}: {e2}")

            await interaction.followup.send(
                f"⚠️ {bold('Something went wrong generating the punishment card.')}",
                ephemeral=True,
            )

            return False

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

    file = discord.File(
        io.BytesIO(card_bytes),
        filename=f"punishment_case_{case_no:04d}.{ext}",
    )

    view = discord.ui.View(timeout=None)
    view.add_item(PunishmentDetailsButton(case_no))

    try:
        await send_with_retry(log_channel, file=file, view=view)

        print(
            f"✅ Case ELT-{case_no:04d} ({ptype}/{render_type}) "
            f"posted for {member} by {interaction.user}"
        )

    except Exception as e:
        print(f"❌ Failed to send punishment card: {e}")

        await interaction.followup.send(
            f"⚠️ {bold('Generated the card but could not post it — check my permissions in that channel.')}",
            ephemeral=True,
        )

        return False

    await interaction.followup.send(
        f"✅ {bold(f'{ptype.title()} logged for')} "
        f"{member.mention} "
        f"{bold('in')} "
        f"{log_channel.mention}.",
        ephemeral=True,
    )

    return True


async def reject(
    interaction: discord.Interaction,
    command: str,
    target: discord.abc.User,
    msg: str
):
    """Sends the ephemeral rejection and logs it."""

    print(f"🚫 /{command}: {interaction.user} -> {target} blocked: {msg}")

    await interaction.followup.send(f"⚠️ {bold(msg)}", ephemeral=True)


# ================================================================
# WARNING EXPIRATION
# ================================================================

async def expire_warnings():
    """If 30 days pass without a new warning, clear the whole active streak."""

    guild = bot.get_guild(GUILD_ID)

    if guild is None:
        return

    now = datetime.now(timezone.utc)

    by_member = {}

    for case_no, record in punishment_records.items():
        if _warning_is_active(record):
            by_member.setdefault(record["user_id"], []).append((case_no, record))

    changed = False

    for member_id, records in by_member.items():

        latest = max(
            records,
            key=lambda item: (
                _parse_warning_time(item[1])
                or datetime.min.replace(tzinfo=timezone.utc),
                item[0],
            ),
        )

        latest_time = _parse_warning_time(latest[1])

        # Old warning records don't have an exact timestamp,
        # so don't guess their expiry.
        if latest_time is None:
            continue

        if now - latest_time < timedelta(days=WARNING_EXPIRE_DAYS):
            continue

        member = guild.get_member(member_id)

        if member is None:
            continue

        cleared_cases = []

        for case_no, record in records:
            record["warning_active"] = False
            record["warning_cleared_at"] = now.isoformat()
            record["warning_clear_method"] = "automatic_30_day_expiry"
            cleared_cases.append(case_no)

        changed = True

        await update_warning_roles(guild, member, 0)

        reason = (
            f"No new warning was received for {WARNING_EXPIRE_DAYS} days. "
            f"All active warnings were automatically cleared."
        )

        punisher = guild.me or bot.user

        if punisher:
            await post_warning_cleared_card(
                guild, member, punisher, reason, cleared_cases
            )

    if changed:
        save_db()


@tasks.loop(hours=1)
async def warning_expiry_loop():
    await expire_warnings()


@warning_expiry_loop.before_loop
async def _wait_ready_for_warning_expiry():
    await bot.wait_until_ready()


# ================================================================
# SLASH COMMAND SYNC
# ================================================================

async def sync_commands() -> bool:
    """Push slash commands to the guild."""

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
    await sync_commands()


@resync_loop.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


@bot.event
async def setup_hook():
    bot.add_dynamic_items(PunishmentDetailsButton)

    resync_loop.start()
    warning_expiry_loop.start()


# ================================================================
# PREFIX SYNC COMMAND
# ================================================================

@bot.command(name="sync")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def sync_cmd(ctx: commands.Context):
    ok = await sync_commands()

    await ctx.reply(
        "✅ Slash commands re-synced."
        if ok
        else "❌ Sync failed, check the logs."
    )


# ================================================================
# READY
# ================================================================

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"👮 Staff role IDs: {sorted(STAFF_ROLE_IDS) or 'none set'}")
    print(f"🛡️ Moderator role ID: {MOD_ROLE_ID}")
    print(f"🔨 Who can ban: {TIER_LABEL[BAN_MIN_TIER]} or higher")
    print(f"⚠️ Warning expiration: {WARNING_EXPIRE_DAYS} days")
    print(f"🟢 Warning cleared channel: {WARNING_CLEAR_CHANNEL_ID}")


@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: commands.CommandError
):
    if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
        return

    print(f"❌ Prefix command error: {error}")


# ================================================================
# /WARN
# ================================================================

@bot.tree.command(
    name="warn",
    description=(
        "Give a warning to a member. "
        "Warnings expire after 30 days without a new warning."
    ),
    guild=GUILD
)
@app_commands.describe(
    member="The member being warned",
    reason="Why they're being warned"
)
@app_commands.guild_only()
@require_tier("staff")
async def warn_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: app_commands.Range[str, 1, 300],
):

    await interaction.response.defer(ephemeral=True)

    problem = target_problem(interaction, member, needs_bot_rank=False)

    if problem:
        return await reject(interaction, "warn", member, problem)

    # Count active warnings (this one included).
    warning_count = active_warning_count(member.id) + 1

    percent = warning_percent(warning_count)

    issued_at = discord.utils.utcnow().isoformat()

    # The automatic ban on the 3rd warning only happens when the person
    # giving the warning is allowed to ban (Administrators by default).
    auto_ban = warning_count >= 3 and can_ban(interaction.user)

    card_type = "BAN" if auto_ban else "WARNING"

    card_reason = reason

    if auto_ban:
        card_reason = (
            f"{reason} — 3 active warnings reached. Automatic ban applied."
        )[:300]

    record_extra = {
        "warning_active": True,
        "warning_issued_at": issued_at,
        "warning_count": warning_count,
        "warning_percent": percent,
    }

    if auto_ban:
        record_extra["automatic_ban"] = True

    # WARNING in punishment_card.py has a default severity of 33%.
    # Temporarily change it to 33 / 66 / 100 for warning 1 / 2 / 3+.
    # The lock prevents two simultaneous warnings from changing it
    # at the same time.

    async with warning_card_style_lock:

        original_warning_style = TYPE_STYLE["WARNING"]

        TYPE_STYLE["WARNING"] = (original_warning_style[0], percent)

        try:
            success = await issue_punishment(
                interaction,
                member,
                "WARNING",
                card_reason,
                card_ptype=card_type,
                record_extra=record_extra,
            )

        finally:
            TYPE_STYLE["WARNING"] = original_warning_style

    if not success:
        # Nothing was saved, so don't touch the warning roles either.
        return

    # Update the warning roles only AFTER the warning was really recorded.
    # Without an automatic ban the member keeps the "Warn 2" role at 3+.
    await update_warning_roles(
        interaction.guild,
        member,
        warning_count if auto_ban else min(warning_count, 2)
    )

    # 3rd warning given by an Administrator = automatic ban.
    if auto_ban:

        try:
            await member.ban(
                reason=audit_reason(
                    f"3 active warnings reached: {reason}",
                    "automatically banned",
                    bot.user,
                )
            )

            print(
                f"🔨 Automatic ban applied to {member} "
                f"after warning #{warning_count}"
            )

        except discord.Forbidden:
            print(
                f"❌ Automatic ban failed for {member}: "
                f"bot lacks ban permission/hierarchy."
            )

            await interaction.followup.send(
                "⚠️ The 3rd-warning BAN card was posted, but Discord refused "
                "the automatic ban. Check my Ban Members permission and "
                "role position.",
                ephemeral=True,
            )

        except discord.HTTPException as e:
            print(f"❌ Automatic ban HTTPException for {member}: {e}")

            await interaction.followup.send(
                f"⚠️ The 3rd-warning BAN card was posted, but the automatic "
                f"ban failed: {e}",
                ephemeral=True,
            )

        return

    # 3rd+ warning from Staff / a Moderator: NO ban. Tell the Administrators.
    if warning_count >= 3:

        admin_role = interaction.guild.get_role(ADMIN_ROLE_ID)

        who = admin_role.mention if admin_role else "Administrators"

        try:
            await get_log_channel(interaction).send(
                f"{who} {member.mention} now has "
                f"**{warning_count}** active warnings. "
                f"An Administrator needs to decide on a ban.",
                allowed_mentions=discord.AllowedMentions(
                    roles=[admin_role] if admin_role else False,
                    users=False,
                    everyone=False,
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

@bot.tree.command(
    name="unwarn",
    description="Remove the most recent active warning from a member.",
    guild=GUILD
)
@app_commands.describe(
    member="The member whose latest warning should be removed",
    reason="Why the warning is being removed",
)
@app_commands.guild_only()
@require_tier("mod")
async def unwarn_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: app_commands.Range[str, 1, 300],
):

    await interaction.response.defer(ephemeral=True)

    problem = target_problem(interaction, member, needs_bot_rank=False)

    if problem:
        return await reject(interaction, "unwarn", member, problem)

    active = active_warning_records(member.id)

    if not active:
        return await interaction.followup.send(
            f"ℹ️ {member.mention} has no active warnings.",
            ephemeral=True,
        )

    # Remove only the newest warning.
    # This is useful for accidental/wrong warnings.
    case_no, record = active[-1]

    record["warning_active"] = False
    record["warning_cleared_at"] = discord.utils.utcnow().isoformat()
    record["warning_cleared_by_id"] = interaction.user.id
    record["warning_cleared_by_tag"] = str(interaction.user)
    record["warning_clear_method"] = "manual_unwarn"
    record["warning_clear_reason"] = reason

    remaining = active_warning_count(member.id)

    # Restore correct role level.
    await update_warning_roles(interaction.guild, member, remaining)

    save_db()

    clear_reason = (
        f"Warning ELT-{case_no:04d} was removed by "
        f"{card_name(interaction.user)}. Reason: {reason}"
    )[:300]

    await post_warning_cleared_card(
        interaction.guild,
        member,
        interaction.user,
        clear_reason,
        [case_no],
    )

    await interaction.followup.send(
        f"🟢 Warning ELT-{case_no:04d} removed from {member.mention}. "
        f"Active warnings remaining: **{remaining}**.",
        ephemeral=True,
    )


# ================================================================
# /MUTE
# ================================================================

@bot.tree.command(
    name="mute",
    description="Time a member out and log a mute card.",
    guild=GUILD
)
@app_commands.describe(
    member="The member being muted",
    minutes="How long to mute them for, in minutes",
    reason="Why they're being muted"
)
@app_commands.guild_only()
@require_tier("mod")
async def mute_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: app_commands.Range[str, 1, 300]
):

    await interaction.response.defer(ephemeral=True)

    problem = target_problem(interaction, member)

    if problem:
        return await reject(interaction, "mute", member, problem)

    try:
        await member.timeout(
            timedelta(minutes=minutes),
            reason=audit_reason(reason, "muted", interaction.user)
        )

    except discord.Forbidden:
        return await reject(
            interaction, "mute", member,
            "I do not have permission to time out that member."
        )

    except discord.HTTPException as e:
        print(f"❌ /mute HTTPException for {member}: {e}")

        return await interaction.followup.send(
            f"⚠️ {bold('Failed to mute:')} {e}",
            ephemeral=True
        )

    await issue_punishment(
        interaction,
        member,
        "MUTE",
        f"{reason} (for {minutes}m)"
    )


# ================================================================
# /TIMEOUT
# ================================================================

@bot.tree.command(
    name="timeout",
    description="Time a member out for minutes, hours or days and log a timeout card.",
    guild=GUILD
)
@app_commands.describe(
    member="The member being timed out",
    amount="How long (a number)",
    unit="Minutes, hours or days",
    reason="Why they're being timed out"
)
@app_commands.choices(
    unit=[
        app_commands.Choice(name="Minutes", value="minutes"),
        app_commands.Choice(name="Hours", value="hours"),
        app_commands.Choice(name="Days", value="days"),
    ]
)
@app_commands.guild_only()
@require_tier("mod")
async def timeout_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: app_commands.Range[int, 1, 40320],
    unit: app_commands.Choice[str],
    reason: app_commands.Range[str, 1, 300]
):

    await interaction.response.defer(ephemeral=True)

    duration = timedelta(**{unit.value: amount})

    if duration > timedelta(days=28):
        return await reject(
            interaction, "timeout", member,
            "Discord only allows timeouts up to 28 days."
        )

    problem = target_problem(interaction, member)

    if problem:
        return await reject(interaction, "timeout", member, problem)

    try:
        await member.timeout(
            duration,
            reason=audit_reason(reason, "timed out", interaction.user)
        )

    except discord.Forbidden:
        return await reject(
            interaction, "timeout", member,
            "I do not have permission to time out that member."
        )

    except discord.HTTPException as e:
        print(f"❌ /timeout HTTPException for {member}: {e}")

        return await interaction.followup.send(
            f"⚠️ {bold('Failed to time out:')} {e}",
            ephemeral=True
        )

    unit_label = unit.value if amount != 1 else unit.value[:-1]

    await issue_punishment(
        interaction,
        member,
        "TIMEOUT",
        f"{reason} (for {amount} {unit_label})"
    )


# ================================================================
# /KICK
# ================================================================

@bot.tree.command(
    name="kick",
    description="Kick a member and log a kick card.",
    guild=GUILD
)
@app_commands.describe(
    member="The member being kicked",
    reason="Why they're being kicked"
)
@app_commands.guild_only()
@require_tier("mod")
async def kick_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: app_commands.Range[str, 1, 300]
):

    await interaction.response.defer(ephemeral=True)

    problem = target_problem(interaction, member)

    if problem:
        return await reject(interaction, "kick", member, problem)

    try:
        await member.kick(
            reason=audit_reason(reason, "kicked", interaction.user)
        )

    except discord.Forbidden:
        return await reject(
            interaction, "kick", member,
            "I do not have permission to kick that member."
        )

    except discord.HTTPException as e:
        print(f"❌ /kick HTTPException for {member}: {e}")

        return await interaction.followup.send(
            f"⚠️ {bold('Failed to kick:')} {e}",
            ephemeral=True
        )

    await issue_punishment(interaction, member, "KICK", reason)


# ================================================================
# /BAN  (also works on someone who already left the server)
# ================================================================

@bot.tree.command(
    name="ban",
    description="Ban a member and log a ban card.",
    guild=GUILD
)
@app_commands.describe(
    member="Who to ban (can be someone who already left: paste their user ID)",
    reason="Why they're being banned"
)
@app_commands.guild_only()
@require_tier(BAN_MIN_TIER)
async def ban_cmd(
    interaction: discord.Interaction,
    member: discord.User,
    reason: app_commands.Range[str, 1, 300]
):

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    in_server = guild.get_member(member.id)

    if in_server is not None:
        problem = target_problem(interaction, in_server)
    elif member.id == interaction.user.id:
        problem = "You can't punish yourself."
    elif bot.user and member.id == bot.user.id:
        problem = "I can't punish myself."
    else:
        problem = None

    if problem:
        return await reject(interaction, "ban", member, problem)

    try:
        await guild.ban(
            member,
            reason=audit_reason(reason, "banned", interaction.user)
        )

    except discord.Forbidden:
        return await reject(
            interaction, "ban", member,
            "I do not have permission to ban that member."
        )

    except discord.HTTPException as e:
        print(f"❌ /ban HTTPException for {member}: {e}")

        return await interaction.followup.send(
            f"⚠️ {bold('Failed to ban:')} {e}",
            ephemeral=True
        )

    await issue_punishment(interaction, member, "BAN", reason)


# ================================================================
# APP COMMAND ERROR
# ================================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    """Prints the full traceback to Railway and tells the user what went wrong."""

    command_name = interaction.command.name if interaction.command else "unknown"

    print("\n" + "=" * 70)
    print("❌ APP COMMAND ERROR")
    print("=" * 70)
    print(f"Command: /{command_name}")
    print(f"User: {interaction.user} ({interaction.user.id})")
    print(f"Error type: {type(error).__name__}")
    print(f"Error: {error}")

    original = getattr(error, "original", None)

    if original is not None:
        print(f"Original error type: {type(original).__name__}")
        print(f"Original error: {original}")

    print("\nFULL TRACEBACK:")
    traceback.print_exception(type(error), error, error.__traceback__)

    if original is not None and original is not error:
        print("\nORIGINAL TRACEBACK:")
        traceback.print_exception(type(original), original, original.__traceback__)

    print("=" * 70 + "\n")

    if isinstance(error, app_commands.MissingPermissions):
        msg = f"⚠️ {bold('You do not have permission to do that.')}"

    elif isinstance(error, NotAllowed):
        msg = f"⚠️ {bold(str(error))}"

    elif isinstance(error, app_commands.TransformerError):
        msg = (
            f"⚠️ {bold('I could not find that member in the server — they may have left.')}"
        )

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
    raise SystemExit(
        "DISCORD_TOKEN is not set. "
        "Add it in Railway's Variables tab, then redeploy."
    )


bot.run(TOKEN)
