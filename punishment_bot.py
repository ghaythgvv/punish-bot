punishment_bot.py
"""
ELT Punishment Bot
=======================================
Slash commands (/warn, /mute, /timeout, /kick, /ban) so staff get Discord's own
UI — a member picker, typed fields, and built-in validation — instead of typing
raw text commands. Wraps punishment_gif.py's render_card_gif() to post an
ANIMATED punishment card with the target's own avatar and display name, plus a
"View Punishment Details" button underneath. If the animated card can't be made
for any reason, it falls back to the still PNG card from punishment_card.py.

Who can use the commands:
    /warn /timeout /kick /ban -> members with the Moderator role (MOD_ROLE_ID) or
                                 any role ABOVE it, plus server Administrators.
    /mute                     -> server Administrators, anyone with the lowest staff
                                 role in STAFF_ROLE_IDS, or anyone above it.

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
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from punishment_card import render_card, TYPE_STYLE
from punishment_gif import render_card_gif, clean_for_card, MAX_BYTES

# =========================== CONFIG ===========================
TOKEN = os.environ.get("DISCORD_TOKEN")

GUILD_ID = 1410440666747633707
GUILD = discord.Object(id=GUILD_ID)

PUNISHMENT_LOG_CHANNEL_ID = None

# Moderator role.
MOD_ROLE_ID = 1513904125086011402

# Warning roles
WARN_1_ROLE_ID = 1513904153900875897
WARN_2_ROLE_ID = 1513904154719027291

# Staff role IDs allowed to use /mute
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
    """Converts normal letters/numbers into Discord mathematical bold."""
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


# ---------------- Case numbers + records ----------------

def load_db():
    try:
        with open(DB_PATH, encoding="utf-8") as f:
            data = json.load(f)

        return (
            int(data.get("counter", 0)),
            {
                int(k): v
                for k, v in data.get("records", {}).items()
            },
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
                {
                    "counter": case_counter,
                    "records": punishment_records,
                },
                f,
                ensure_ascii=False,
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
    return clean_for_card(member.display_name) or member.name


def audit_reason(
    reason: str,
    action: str,
    by: discord.abc.User
) -> str:
    return f"{reason} — {action} by {by}"[:512]


def staff_only(permission: str):
    async def predicate(
        interaction: discord.Interaction
    ) -> bool:

        user = interaction.user

        if user.guild_permissions.administrator:
            return True

        staff_roles = [
            r
            for r in (
                interaction.guild.get_role(i)
                for i in STAFF_ROLE_IDS
            )
            if r
        ]

        if staff_roles and user.top_role >= min(staff_roles):
            return True

        raise app_commands.MissingPermissions([permission])

    return app_commands.check(predicate)


def mod_only(permission: str):
    async def predicate(
        interaction: discord.Interaction
    ) -> bool:

        user = interaction.user

        if user.guild_permissions.administrator:
            return True

        mod_role = interaction.guild.get_role(MOD_ROLE_ID)

        if mod_role and user.top_role >= mod_role:
            return True

        raise app_commands.MissingPermissions([permission])

    return app_commands.check(predicate)


def target_problem(
    interaction: discord.Interaction,
    member: discord.Member,
    needs_bot_rank: bool = True
):
    guild = interaction.guild

    if member.id == interaction.user.id:
        return "You can't punish yourself."

    if member.id == bot.user.id:
        return "I can't punish myself."

    if member.id == guild.owner_id:
        return "You can't punish the server owner."

    if (
        interaction.user.id != guild.owner_id
        and member.top_role >= interaction.user.top_role
    ):
        return "That member's highest role is equal to or above yours."

    if (
        needs_bot_rank
        and member.top_role >= guild.me.top_role
    ):
        return (
            "That member's highest role is equal to or above mine — "
            "move my role higher."
        )

    return None


async def send_with_retry(channel, **kwargs):
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
                    print(
                        f"❌ Failed to send message "
                        f"after {max_attempts} attempts"
                    )
                    raise

            else:
                raise


class PunishmentDetailsButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"punishment_details_(?P<case>[0-9]+)",
):

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


    async def callback(
        self,
        interaction: discord.Interaction
    ):

        record = punishment_records.get(self.case_no)

        if record is None:
            return await interaction.response.send_message(
                bold(
                    "Details for this case aren't "
                    "available anymore."
                ),
                ephemeral=True,
            )

        color_rgb = TYPE_STYLE[record["type"]][0]

        embed = discord.Embed(
            title=bold(
                f"Case ELT-{self.case_no:04d} — "
                f"{record['type'].title()}"
            ),
            color=discord.Color.from_rgb(*color_rgb),
        )

        embed.add_field(
            name=bold("User"),
            value=(
                f"<@{record['user_id']}> "
                f"({bold(record['user_tag'])})"
            ),
            inline=False,
        )

        embed.add_field(
            name=bold("Punisher"),
            value=(
                f"<@{record['punisher_id']}> "
                f"({bold(record['punisher_tag'])})"
            ),
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

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )


# ================================================================
# PUNISHMENT SYSTEM
# ================================================================

async def issue_punishment(
    interaction: discord.Interaction,
    member: discord.Member,
    ptype: str,
    reason: str
):
    """
    Creates the punishment card.

    WARNING SYSTEM:
        1 warning = Warn 1 role = 33%
        2 warnings = Warn 2 role = 66%
        3 warnings = 100% + automatic ban
    """

    global case_counter

    warning_count = 0
    warning_percent = None


    # ============================================================
    # WARNING SYSTEM
    # ============================================================

    if ptype == "WARNING":

        # Count previous warnings for this member.
        warning_count = sum(
            1
            for record in punishment_records.values()
            if record.get("user_id") == member.id
            and record.get("type") == "WARNING"
        ) + 1

        warning_percent = min(
            warning_count * 33,
            100
        )

        warn_1_role = interaction.guild.get_role(
            WARN_1_ROLE_ID
        )

        warn_2_role = interaction.guild.get_role(
            WARN_2_ROLE_ID
        )


        try:

            # ====================================================
            # FIRST WARNING
            # ====================================================

            if warning_count == 1:

                if (
                    warn_2_role
                    and warn_2_role in member.roles
                ):
                    await member.remove_roles(
                        warn_2_role,
                        reason="Warning level set to Warning 1",
                    )

                if (
                    warn_1_role
                    and warn_1_role not in member.roles
                ):
                    await member.add_roles(
                        warn_1_role,
                        reason="1st warning",
                    )


            # ====================================================
            # SECOND WARNING
            # ====================================================

            elif warning_count == 2:

                if (
                    warn_1_role
                    and warn_1_role in member.roles
                ):
                    await member.remove_roles(
                        warn_1_role,
                        reason="Warning level increased to Warning 2",
                    )

                if (
                    warn_2_role
                    and warn_2_role not in member.roles
                ):
                    await member.add_roles(
                        warn_2_role,
                        reason="2nd warning",
                    )


            # ====================================================
            # THIRD WARNING
            # ====================================================

            elif warning_count >= 3:

                if (
                    warn_1_role
                    and warn_1_role in member.roles
                ):
                    await member.remove_roles(
                        warn_1_role,
                        reason="3 warnings reached",
                    )

                if (
                    warn_2_role
                    and warn_2_role in member.roles
                ):
                    await member.remove_roles(
                        warn_2_role,
                        reason="3 warnings reached",
                    )


        except discord.Forbidden:

            print(
                f"❌ Bot cannot manage warning roles for {member}. "
                f"Move the bot role ABOVE Warn 1 and Warn 2."
            )

        except discord.HTTPException as e:

            print(
                f"❌ Warning role update failed "
                f"for {member}: {e}"
            )


    # ============================================================
    # CASE NUMBER
    # ============================================================

    case_counter += 1

    case_no = case_counter

    save_db()


    # ============================================================
    # AVATAR
    # ============================================================

    try:

        avatar_bytes = await (
            member.display_avatar
            .replace(
                size=256,
                format="png"
            )
            .read()
        )

    except Exception as e:

        print(
            f"⚠️ Couldn't fetch avatar for {member}: {e}"
        )

        avatar_bytes = None


    date_text = discord.utils.utcnow().strftime(
        "%d/%m/%Y"
    )

    user_name = card_name(member)

    punisher_name = card_name(
        interaction.user
    )

    max_bytes = min(
        MAX_BYTES,
        int(
            interaction.guild.filesize_limit
            * 0.9
        ),
    )


    # ============================================================
    # WARNING SEVERITY
    # ============================================================

    original_warning_style = TYPE_STYLE.get(
        "WARNING"
    )

    if (
        ptype == "WARNING"
        and warning_percent is not None
    ):

        warning_color = (
            original_warning_style[0]
        )

        TYPE_STYLE["WARNING"] = (
            warning_color,
            warning_percent,
        )


    # ============================================================
    # GENERATE CARD
    # ============================================================

    try:

        try:

            card_bytes = await asyncio.to_thread(
                render_card_gif,
                user_name,
                punisher_name,
                reason,
                ptype,
                case_no,
                date_text,
                avatar_bytes,
                max_bytes,
            )

            ext = "gif"


        except Exception as e:

            print(
                f"⚠️ Animated card failed "
                f"({type(e).__name__}: {e}) "
                f"— falling back to the still card"
            )

            card_bytes = await asyncio.to_thread(
                render_card,
                user_name,
                punisher_name,
                reason,
                ptype,
                case_no,
                date_text,
                avatar_bytes,
            )

            ext = "png"


    except Exception as e:

        print(
            f"❌ Failed to render punishment card: {e}"
        )

        if original_warning_style is not None:
            TYPE_STYLE["WARNING"] = (
                original_warning_style
            )

        await interaction.followup.send(
            f"⚠️ {bold('Something went wrong generating the punishment card.')}",
            ephemeral=True,
        )

        return


    # Restore normal warning style.

    if original_warning_style is not None:
        TYPE_STYLE["WARNING"] = (
            original_warning_style
        )


    # ============================================================
    # SAVE RECORD
    # ============================================================

    punishment_records[case_no] = {

        "user_id": member.id,

        "user_tag": str(member),

        "punisher_id": interaction.user.id,

        "punisher_tag": str(
            interaction.user
        ),

        "reason": reason,

        "type": ptype,

        "date_text": date_text,

        "warning_count": (
            warning_count
            if ptype == "WARNING"
            else None
        ),

        "warning_percent": (
            warning_percent
            if ptype == "WARNING"
            else None
        ),
    }

    save_db()


    # ============================================================
    # LOG CHANNEL
    # ============================================================

    log_channel = (
        interaction.guild.get_channel(
            PUNISHMENT_LOG_CHANNEL_ID
        )
        if PUNISHMENT_LOG_CHANNEL_ID
        else interaction.channel
    )

    if log_channel is None:
        log_channel = interaction.channel


    file = discord.File(
        io.BytesIO(card_bytes),
        filename=(
            f"punishment_case_"
            f"{case_no:04d}.{ext}"
        ),
    )


    view = discord.ui.View(
        timeout=None
    )

    view.add_item(
        PunishmentDetailsButton(
            case_no
        )
    )


    # ============================================================
    # SEND CARD
    # ============================================================

    try:

        await send_with_retry(
            log_channel,
            file=file,
            view=view,
        )

        print(
            f"✅ Case ELT-{case_no:04d} "
            f"({ptype}) posted for {member} "
            f"by {interaction.user}"
        )


    except Exception as e:

        print(
            f"❌ Failed to send punishment card: {e}"
        )

        await interaction.followup.send(
            f"⚠️ {bold('Generated the card but could not post it — check my permissions in that channel.')}",
            ephemeral=True,
        )

        return


    # ============================================================
    # THIRD WARNING = AUTOMATIC BAN
    # ============================================================

    if (
        ptype == "WARNING"
        and warning_count >= 3
    ):

        try:

            await member.ban(
                reason=audit_reason(
                    f"{reason} — 3 warnings reached",
                    "automatically banned",
                    interaction.user,
                )
            )

            print(
                f"🔨 {member} automatically "
                f"banned after "
                f"{warning_count} warnings."
            )

            await interaction.followup.send(
                f"🔨 {bold('3 warnings reached.')} "
                f"{member.mention} "
                f"{bold('has been automatically banned.')}",
                ephemeral=True,
            )


        except discord.Forbidden:

            print(
                f"❌ I could not ban {member}. "
                f"Check Ban Members permission "
                f"and bot role position."
            )

            await interaction.followup.send(
                f"⚠️ {bold('3 warnings reached, but I could not ban the member. Check my role position and Ban Members permission.')}",
                ephemeral=True,
            )


        except discord.HTTPException as e:

            print(
                f"❌ Failed to automatically ban "
                f"{member}: {e}"
            )

            await interaction.followup.send(
                f"⚠️ {bold('3 warnings reached, but the automatic ban failed.')} `{e}`",
                ephemeral=True,
            )

        return


    # ============================================================
    # NORMAL RESPONSE
    # ============================================================

    if ptype == "WARNING":

        await interaction.followup.send(

            f"⚠️ "
            f"{bold(f'Warning {warning_count}/3 logged for')} "
            f"{member.mention} "
            f"{bold(f'— {warning_percent}% severity.')}",

            ephemeral=True,
        )

    else:

        await interaction.followup.send(

            f"✅ "
            f"{bold(f'{ptype.title()} logged for')} "
            f"{member.mention} "
            f"{bold('in')} "
            f"{log_channel.mention}.",

            ephemeral=True,
        )


async def reject(
    interaction: discord.Interaction,
    command: str,
    target: discord.Member,
    msg: str
):
    print(
        f"🚫 /{command}: "
        f"{interaction.user} -> "
        f"{target} blocked: {msg}"
    )

    await interaction.followup.send(
        f"⚠️ {bold(msg)}",
        ephemeral=True
    )


# ================================================================
# SLASH COMMAND SYNC
# ================================================================

async def sync_commands() -> bool:

    for attempt in range(1, 6):

        try:

            synced = await bot.tree.sync(
                guild=GUILD
            )

            print(
                f"🔄 Synced {len(synced)} "
                f"slash command(s) to guild "
                f"{GUILD_ID}"
            )

            return True

        except Exception as e:

            print(
                f"❌ Sync failed "
                f"(attempt {attempt}/5): {e}"
            )

            await asyncio.sleep(
                5 * attempt
            )

    return False


@tasks.loop(hours=6)
async def resync_loop():

    await sync_commands()


@resync_loop.before_loop
async def _wait_ready():

    await bot.wait_until_ready()


@bot.event
async def setup_hook():

    bot.add_dynamic_items(
        PunishmentDetailsButton
    )

    resync_loop.start()


@bot.command(name="sync")
@commands.guild_only()
@commands.has_guild_permissions(
    administrator=True
)
async def sync_cmd(
    ctx: commands.Context
):

    ok = await sync_commands()

    await ctx.reply(
        "✅ Slash commands re-synced."
        if ok
        else
        "❌ Sync failed, check the logs."
    )


@bot.event
async def on_ready():

    print(
        f"✅ Logged in as {bot.user} "
        f"(ID: {bot.user.id})"
    )

    print(
        f"👮 Staff role IDs: "
        f"{sorted(STAFF_ROLE_IDS) or 'none set'}"
    )

    print(
        f"🛡️ Moderator role ID: "
        f"{MOD_ROLE_ID}"
    )


@bot.event
async def on_command_error(
    ctx: commands.Context,
    error: commands.CommandError
):

    if isinstance(
        error,
        (
            commands.CommandNotFound,
            commands.CheckFailure
        )
    ):
        return

    print(
        f"❌ Prefix command error: {error}"
    )


# ================================================================
# /WARN
# ================================================================

@bot.tree.command(
    name="warn",
    description="Warn a member and update their warning level.",
    guild=GUILD
)
@app_commands.describe(
    member="The member being warned",
    reason="Why they're being warned"
)
@app_commands.guild_only()
@mod_only("manage_messages")
async def warn_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: app_commands.Range[
        str,
        1,
        300
    ]
):

    await interaction.response.defer(
        ephemeral=True
    )

    problem = target_problem(
        interaction,
        member,
        needs_bot_rank=False
    )

    if problem:

        return await reject(
            interaction,
            "warn",
            member,
            problem
        )

    await issue_punishment(
        interaction,
        member,
        "WARNING",
        reason
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
@staff_only("moderate_members")
async def mute_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[
        int,
        1,
        40320
    ],
    reason: app_commands.Range[
        str,
        1,
        300
    ]
):

    await interaction.response.defer(
        ephemeral=True
    )

    problem = target_problem(
        interaction,
        member
    )

    if problem:

        return await reject(
            interaction,
            "mute",
            member,
            problem
        )

    try:

        await member.timeout(
            timedelta(
                minutes=minutes
            ),
            reason=audit_reason(
                reason,
                "muted",
                interaction.user
            )
        )

    except discord.Forbidden:

        return await reject(
            interaction,
            "mute",
            member,
            "I do not have permission to time out that member."
        )

    except discord.HTTPException as e:

        print(
            f"❌ /mute HTTPException "
            f"for {member}: {e}"
        )

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
        app_commands.Choice(
            name="Minutes",
            value="minutes"
        ),
        app_commands.Choice(
            name="Hours",
            value="hours"
        ),
        app_commands.Choice(
            name="Days",
            value="days"
        ),
    ]
)
@app_commands.guild_only()
@mod_only("moderate_members")
async def timeout_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: app_commands.Range[
        int,
        1,
        40320
    ],
    unit: app_commands.Choice[str],
    reason: app_commands.Range[
        str,
        1,
        300
    ]
):

    await interaction.response.defer(
        ephemeral=True
    )

    duration = timedelta(
        **{
            unit.value: amount
        }
    )

    if duration > timedelta(
        days=28
    ):

        return await reject(
            interaction,
            "timeout",
            member,
            "Discord only allows timeouts up to 28 days."
        )

    problem = target_problem(
        interaction,
        member
    )

    if problem:

        return await reject(
            interaction,
            "timeout",
            member,
            problem
        )

    try:

        await member.timeout(
            duration,
            reason=audit_reason(
                reason,
                "timed out",
                interaction.user
            )
        )

    except discord.Forbidden:

        return await reject(
            interaction,
            "timeout",
            member,
            "I do not have permission to time out that member."
        )

    except discord.HTTPException as e:

        print(
            f"❌ /timeout HTTPException "
            f"for {member}: {e}"
        )

        return await interaction.followup.send(
            f"⚠️ {bold('Failed to time out:')} {e}",
            ephemeral=True
        )

    unit_label = (
        unit.value
        if amount != 1
        else unit.value[:-1]
    )

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
@mod_only("kick_members")
async def kick_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: app_commands.Range[
        str,
        1,
        300
    ]
):

    await interaction.response.defer(
        ephemeral=True
    )

    problem = target_problem(
        interaction,
        member
    )

    if problem:

        return await reject(
            interaction,
            "kick",
            member,
            problem
        )

    try:

        await member.kick(
            reason=audit_reason(
                reason,
                "kicked",
                interaction.user
            )
        )

    except discord.Forbidden:

        return await reject(
            interaction,
            "kick",
            member,
            "I do not have permission to kick that member."
        )

    except discord.HTTPException as e:

        print(
            f"❌ /kick HTTPException "
            f"for {member}: {e}"
        )

        return await interaction.followup.send(
            f"⚠️ {bold('Failed to kick:')} {e}",
            ephemeral=True
        )

    await issue_punishment(
        interaction,
        member,
        "KICK",
        reason
    )


# ================================================================
# /BAN
# ================================================================

@bot.tree.command(
    name="ban",
    description="Ban a member and log a ban card.",
    guild=GUILD
)
@app_commands.describe(
    member="The member being banned",
    reason="Why they're being banned"
)
@app_commands.guild_only()
@mod_only("ban_members")
async def ban_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: app_commands.Range[
        str,
        1,
        300
    ]
):

    await interaction.response.defer(
        ephemeral=True
    )

    problem = target_problem(
        interaction,
        member
    )

    if problem:

        return await reject(
            interaction,
            "ban",
            member,
            problem
        )

    try:

        await member.ban(
            reason=audit_reason(
                reason,
                "banned",
                interaction.user
            )
        )

    except discord.Forbidden:

        return await reject(
            interaction,
            "ban",
            member,
            "I do not have permission to ban that member."
        )

    except discord.HTTPException as e:

        print(
            f"❌ /ban HTTPException "
            f"for {member}: {e}"
        )

        return await interaction.followup.send(
            f"⚠️ {bold('Failed to ban:')} {e}",
            ephemeral=True
        )

    await issue_punishment(
        interaction,
        member,
        "BAN",
        reason
    )


# ================================================================
# APP COMMAND ERROR
# ================================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    if isinstance(
        error,
        app_commands.MissingPermissions
    ):

        print(
            f"🚫 {interaction.user} tried "
            f"/{interaction.command.name if interaction.command else '?'} "
            f"without permission"
        )

        msg = (
            f"⚠️ "
            f"{bold('You do not have permission to do that.')}"
        )

    else:

        original = getattr(
            error,
            "original",
            error
        )

        print(
            f"❌ Unhandled app command error "
            f"in {interaction.command}: "
            f"{original}"
        )

        msg = (
            f"⚠️ "
            f"{bold('Something went wrong running that command.')}"
        )


    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                msg,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                msg,
                ephemeral=True
            )

    except discord.HTTPException:
        pass


# ================================================================
# START BOT
# ================================================================

if not TOKEN:

    raise SystemExit(
        "DISCORD_TOKEN is not set. "
        "Add it in Railway's Variables tab, "
        "then redeploy."
    )


bot.run(TOKEN)
