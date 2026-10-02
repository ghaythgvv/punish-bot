"""
ELT Punishment Bot
=======================================
Slash commands (/warn, /unwarn, /mute, /timeout, /kick, /ban)

Warning system:
    1 warning  -> Warn 1 role -> 33%
    2 warnings -> Warn 2 role -> 66%
    3 warnings -> 100% -> automatic BAN

Warning expiration:
    If the member receives NO new warning for 30 days,
    all active warnings are cleared automatically.

Manual warning removal:
    /unwarn member reason

WARNING CLEARED cards are sent to:
    1540154905644367893
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

# Channel where WARNING CLEARED cards are sent.
WARNING_CLEARED_CHANNEL_ID = 1540154905644367893

# Moderator role.
MOD_ROLE_ID = 1513904125086011402

# Warning roles.
WARN_1_ROLE_ID = 1513904153900875897
WARN_2_ROLE_ID = 1513904154719027291

# Warning expiration.
WARNING_EXPIRE_DAYS = 30

# Staff role IDs allowed to use /mute.
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
            out.append(
                chr(_BOLD_UPPER_START + (code - 65))
            )

        elif 97 <= code <= 122:
            out.append(
                chr(_BOLD_LOWER_START + (code - 97))
            )

        elif 48 <= code <= 57:
            out.append(
                chr(_BOLD_DIGIT_START + (code - 48))
            )

        else:
            out.append(ch)

    return "".join(out)


# ================================================================
# DATABASE
# ================================================================

def load_db():

    try:

        with open(DB_PATH, encoding="utf-8") as f:
            data = json.load(f)

        return (
            int(data.get("counter", 0)),
            {
                int(k): v
                for k, v in data.get(
                    "records",
                    {}
                ).items()
            },
        )

    except FileNotFoundError:

        return 0, {}

    except Exception as e:

        print(
            f"⚠️ Couldn't read {DB_PATH}: {e}"
        )

        return 0, {}


def save_db():

    try:

        os.makedirs(
            os.path.dirname(DB_PATH),
            exist_ok=True
        )

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

        os.replace(
            tmp,
            DB_PATH
        )

    except Exception as e:

        print(
            f"❌ Couldn't save {DB_PATH}: {e}"
        )


case_counter, punishment_records = load_db()


print(
    f"📁 Case data: {DB_PATH} "
    f"(last case #{case_counter:04d}, "
    f"{len(punishment_records)} saved)"
)


# ================================================================
# HELPERS
# ================================================================

def card_name(member: discord.abc.User) -> str:

    return (
        clean_for_card(member.display_name)
        or member.name
    )


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

        if (
            staff_roles
            and user.top_role >= min(staff_roles)
        ):
            return True

        raise app_commands.MissingPermissions(
            [permission]
        )

    return app_commands.check(predicate)


def mod_only(permission: str):

    async def predicate(
        interaction: discord.Interaction
    ) -> bool:

        user = interaction.user

        if user.guild_permissions.administrator:
            return True

        mod_role = interaction.guild.get_role(
            MOD_ROLE_ID
        )

        if (
            mod_role
            and user.top_role >= mod_role
        ):
            return True

        raise app_commands.MissingPermissions(
            [permission]
        )

    return app_commands.check(predicate)


def target_problem(
    interaction: discord.Interaction,
    member: discord.Member,
    needs_bot_rank: bool = True
):

    guild = interaction.guild

    if member.id == interaction.user.id:
        return "You can't punish yourself."

    if bot.user and member.id == bot.user.id:
        return "I can't punish myself."

    if member.id == guild.owner_id:
        return "You can't punish the server owner."

    if (
        interaction.user.id != guild.owner_id
        and member.top_role >= interaction.user.top_role
    ):
        return (
            "That member's highest role is "
            "equal to or above yours."
        )

    if (
        needs_bot_rank
        and member.top_role >= guild.me.top_role
    ):
        return (
            "That member's highest role is "
            "equal to or above mine — "
            "move my role higher."
        )

    return None


async def send_with_retry(
    channel,
    **kwargs
):

    max_attempts = 5
    backoff_delays = [
        1,
        2,
        4,
        8,
        16
    ]

    for attempt in range(max_attempts):

        try:

            return await channel.send(
                **kwargs
            )

        except discord.errors.HTTPException as e:

            if e.status == 429:

                if attempt < max_attempts - 1:

                    delay = backoff_delays[
                        attempt
                    ]

                    print(
                        f"⏳ Rate limited, "
                        f"retrying in {delay}s "
                        f"(attempt "
                        f"{attempt + 1}/"
                        f"{max_attempts})"
                    )

                    await asyncio.sleep(
                        delay
                    )

                else:

                    print(
                        f"❌ Failed to send "
                        f"message after "
                        f"{max_attempts} attempts"
                    )

                    raise

            else:

                raise


# ================================================================
# WARNING HELPERS
# ================================================================

def is_active_warning(record):

    return (
        record.get("type") == "WARNING"
        and record.get(
            "warning_active",
            True
        )
    )


def get_active_warnings(member_id):

    records = [
        (case_no, record)
        for case_no, record
        in punishment_records.items()
        if (
            record.get("user_id") == member_id
            and is_active_warning(record)
        )
    ]

    records.sort(
        key=lambda x: x[0]
    )

    return records


def active_warning_count(member_id):

    return len(
        get_active_warnings(
            member_id
        )
    )


def warning_percent(count):

    if count <= 0:
        return 0

    if count == 1:
        return 33

    if count == 2:
        return 66

    return 100


async def update_warning_roles(
    member: discord.Member,
    count: int,
    reason: str
):

    warn_1_role = member.guild.get_role(
        WARN_1_ROLE_ID
    )

    warn_2_role = member.guild.get_role(
        WARN_2_ROLE_ID
    )

    try:

        if count <= 0:

            if (
                warn_1_role
                and warn_1_role in member.roles
            ):

                await member.remove_roles(
                    warn_1_role,
                    reason=reason
                )

            if (
                warn_2_role
                and warn_2_role in member.roles
            ):

                await member.remove_roles(
                    warn_2_role,
                    reason=reason
                )

        elif count == 1:

            if (
                warn_2_role
                and warn_2_role in member.roles
            ):

                await member.remove_roles(
                    warn_2_role,
                    reason=reason
                )

            if (
                warn_1_role
                and warn_1_role not in member.roles
            ):

                await member.add_roles(
                    warn_1_role,
                    reason=reason
                )

        else:

            if (
                warn_1_role
                and warn_1_role in member.roles
            ):

                await member.remove_roles(
                    warn_1_role,
                    reason=reason
                )

            if (
                warn_2_role
                and warn_2_role not in member.roles
            ):

                await member.add_roles(
                    warn_2_role,
                    reason=reason
                )

    except discord.Forbidden:

        print(
            "❌ Cannot manage warning roles. "
            "Make sure the bot role is ABOVE "
            "Warn 1 and Warn 2."
        )

    except discord.HTTPException as e:

        print(
            f"❌ Warning role error: {e}"
        )


# ================================================================
# DETAILS BUTTON
# ================================================================

class PunishmentDetailsButton(
    discord.ui.DynamicItem[
        discord.ui.Button
    ],
    template=r"punishment_details_(?P<case>[0-9]+)",
):

    def __init__(
        self,
        case_no: int
    ):

        super().__init__(
            discord.ui.Button(
                label=(
                    f"🔍 "
                    f"{bold('View Punishment Details')}"
                ),
                style=discord.ButtonStyle.secondary,
                custom_id=(
                    f"punishment_details_{case_no}"
                ),
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

        return cls(
            int(match["case"])
        )

    async def callback(
        self,
        interaction: discord.Interaction
    ):

        record = punishment_records.get(
            self.case_no
        )

        if record is None:

            return await interaction.response.send_message(
                bold(
                    "Details for this case aren't "
                    "available anymore."
                ),
                ephemeral=True,
            )

        record_type = record.get(
            "type",
            "WARNING"
        )

        color_data = TYPE_STYLE.get(
            record_type,
            TYPE_STYLE.get("WARNING")
        )

        color_rgb = color_data[0]

        embed = discord.Embed(
            title=bold(
                f"Case ELT-{self.case_no:04d} — "
                f"{record_type.title()}"
            ),
            color=discord.Color.from_rgb(
                *color_rgb
            ),
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
            value=bold(
                record["reason"]
            ),
            inline=False,
        )

        embed.add_field(
            name=bold("Date"),
            value=bold(
                record["date_text"]
            ),
            inline=True,
        )

        if record_type == "WARNING":

            embed.add_field(
                name=bold("Warning Level"),
                value=bold(
                    f"{record.get('warning_count', 1)}/3 "
                    f"({record.get('warning_percent', 33)}%)"
                ),
                inline=True,
            )

        if record_type == "WARNING CLEARED":

            embed.add_field(
                name=bold("Status"),
                value=bold(
                    "Warning removed / cleared"
                ),
                inline=True,
            )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )


# ================================================================
# CARD RENDERING
# ================================================================

async def render_punishment_card(
    member,
    punisher,
    reason,
    ptype,
    case_no,
    date_text,
    warning_percent=None
):

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
            f"⚠️ Couldn't fetch avatar "
            f"for {member}: {e}"
        )

        avatar_bytes = None

    max_bytes = min(
        MAX_BYTES,
        int(
            member.guild.filesize_limit
            * 0.9
        )
    )

    # WARNING cards need exact levels:
    #
    # 1 = 33%
    # 2 = 66%
    # 3 = 100%
    #
    # WARNING CLEARED uses its own green style.

    original_warning_style = TYPE_STYLE.get(
        "WARNING"
    )

    original_cleared_style = TYPE_STYLE.get(
        "WARNING CLEARED"
    )

    if (
        ptype == "WARNING"
        and warning_percent is not None
    ):

        TYPE_STYLE["WARNING"] = (
            original_warning_style[0]
            if original_warning_style
            else (255, 176, 32),
            warning_percent
        )

    elif ptype == "WARNING CLEARED":

        # Green style for the cleared card.
        TYPE_STYLE["WARNING CLEARED"] = (
            (60, 220, 120),
            100
        )

    try:

        card_bytes = await asyncio.to_thread(
            render_card_gif,
            card_name(member),
            card_name(punisher),
            reason,
            ptype,
            case_no,
            date_text,
            avatar_bytes,
            max_bytes
        )

        ext = "gif"

    except Exception as e:

        print(
            f"⚠️ Animated card failed "
            f"({type(e).__name__}: {e}) "
            f"— falling back to still card"
        )

        try:

            card_bytes = await asyncio.to_thread(
                render_card,
                card_name(member),
                card_name(punisher),
                reason,
                ptype,
                case_no,
                date_text,
                avatar_bytes
            )

            ext = "png"

        except Exception as e2:

            print(
                f"❌ Failed to render "
                f"punishment card: {e2}"
            )

            if original_warning_style is not None:

                TYPE_STYLE["WARNING"] = (
                    original_warning_style
                )

            if original_cleared_style is not None:

                TYPE_STYLE["WARNING CLEARED"] = (
                    original_cleared_style
                )

            else:

                TYPE_STYLE.pop(
                    "WARNING CLEARED",
                    None
                )

            raise

    # Restore styles.

    if original_warning_style is not None:

        TYPE_STYLE["WARNING"] = (
            original_warning_style
        )

    if original_cleared_style is not None:

        TYPE_STYLE["WARNING CLEARED"] = (
            original_cleared_style
        )

    else:

        TYPE_STYLE.pop(
            "WARNING CLEARED",
            None
        )

    return card_bytes, ext


# ================================================================
# POST CARD
# ================================================================

async def post_card(
    interaction,
    member,
    ptype,
    reason,
    case_no,
    warning_percent=None,
    channel=None
):

    date_text = discord.utils.utcnow().strftime(
        "%d/%m/%Y"
    )

    try:

        card_bytes, ext = await render_punishment_card(
            member,
            interaction.user,
            reason,
            ptype,
            case_no,
            date_text,
            warning_percent
        )

    except Exception:

        await interaction.followup.send(
            f"⚠️ {bold('Something went wrong generating the punishment card.')}",
            ephemeral=True
        )

        return False

    if channel is None:
        channel = interaction.channel

    file = discord.File(
        io.BytesIO(card_bytes),
        filename=(
            f"punishment_case_"
            f"{case_no:04d}.{ext}"
        )
    )

    view = discord.ui.View(
        timeout=None
    )

    view.add_item(
        PunishmentDetailsButton(
            case_no
        )
    )

    try:

        await send_with_retry(
            channel,
            file=file,
            view=view
        )

        return True

    except Exception as e:

        print(
            f"❌ Failed to send punishment card: {e}"
        )

        return False


# ================================================================
# SAVE WARNING CLEAR RECORD
# ================================================================

async def create_warning_cleared_card(
    member: discord.Member,
    removed_count: int,
    reason: str,
    removed_by
):

    global case_counter

    case_counter += 1

    case_no = case_counter

    date_text = discord.utils.utcnow().strftime(
        "%d/%m/%Y"
    )

    clear_reason = (
        f"{reason} "
        f"— {removed_count} warning(s) cleared."
    )

    # Fetch avatar.

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
            f"⚠️ Couldn't fetch avatar "
            f"for cleared warning: {e}"
        )

        avatar_bytes = None

    max_bytes = min(
        MAX_BYTES,
        int(
            member.guild.filesize_limit
            * 0.9
        )
    )

    # Add temporary green style.
    original_style = TYPE_STYLE.get(
        "WARNING CLEARED"
    )

    TYPE_STYLE["WARNING CLEARED"] = (
        (60, 220, 120),
        100
    )

    try:

        try:

            card_bytes = await asyncio.to_thread(
                render_card_gif,
                card_name(member),
                card_name(removed_by),
                clear_reason,
                "WARNING CLEARED",
                case_no,
                date_text,
                avatar_bytes,
                max_bytes
            )

            ext = "gif"

        except Exception as e:

            print(
                f"⚠️ Warning-cleared GIF failed: {e}"
            )

            card_bytes = await asyncio.to_thread(
                render_card,
                card_name(member),
                card_name(removed_by),
                clear_reason,
                "WARNING CLEARED",
                case_no,
                date_text,
                avatar_bytes
            )

            ext = "png"

    finally:

        if original_style is not None:

            TYPE_STYLE["WARNING CLEARED"] = (
                original_style
            )

        else:

            TYPE_STYLE.pop(
                "WARNING CLEARED",
                None
            )

    punishment_records[case_no] = {

        "user_id": member.id,

        "user_tag": str(member),

        "punisher_id": removed_by.id,

        "punisher_tag": str(removed_by),

        "reason": clear_reason,

        "type": "WARNING CLEARED",

        "date_text": date_text,

        "warning_count": 0,

        "warning_percent": 0,

        "warning_active": False,
    }

    save_db()

    channel = member.guild.get_channel(
        WARNING_CLEARED_CHANNEL_ID
    )

    if channel is None:

        print(
            f"❌ WARNING_CLEARED_CHANNEL_ID "
            f"{WARNING_CLEARED_CHANNEL_ID} "
            f"was not found."
        )

        return

    file = discord.File(
        io.BytesIO(card_bytes),
        filename=(
            f"warning_cleared_"
            f"{case_no:04d}.{ext}"
        )
    )

    view = discord.ui.View(
        timeout=None
    )

    view.add_item(
        PunishmentDetailsButton(
            case_no
        )
    )

    try:

        await send_with_retry(
            channel,
            file=file,
            view=view
        )

        print(
            f"🟢 Warning cleared card "
            f"posted for {member}"
        )

    except Exception as e:

        print(
            f"❌ Failed to post "
            f"WARNING CLEARED card: {e}"
        )


# ================================================================
# CLEAR ALL WARNINGS
# ================================================================

async def clear_all_warnings(
    member: discord.Member,
    reason: str,
    removed_by
):

    warnings = get_active_warnings(
        member.id
    )

    if not warnings:
        return 0

    for case_no, record in warnings:

        record["warning_active"] = False

        record["cleared_at"] = (
            discord.utils.utcnow().isoformat()
        )

        record["cleared_reason"] = reason

    save_db()

    await update_warning_roles(
        member,
        0,
        reason
    )

    await create_warning_cleared_card(
        member,
        len(warnings),
        reason,
        removed_by
    )

    return len(warnings)


# ================================================================
# AUTOMATIC 30-DAY WARNING EXPIRATION
# ================================================================

@tasks.loop(hours=1)
async def warning_expiry_loop():

    now = discord.utils.utcnow()

    guild = bot.get_guild(
        GUILD_ID
    )

    if guild is None:
        return

    members_to_clear = []

    # Find members with active warnings.

    member_ids = {
        record.get("user_id")
        for record in punishment_records.values()
        if is_active_warning(record)
    }

    for member_id in member_ids:

        warnings = get_active_warnings(
            member_id
        )

        if not warnings:
            continue

        # We reset the 30-day timer whenever a
        # new warning is issued.
        #
        # Find the newest warning with a
        # stored issued_at timestamp.

        newest_time = None

        for _, record in warnings:

            issued_at = record.get(
                "issued_at"
            )

            if not issued_at:
                continue

            try:

                timestamp = (
                    discord.utils.parse_time(
                        issued_at
                    )
                )

            except Exception:

                try:

                    timestamp = (
                        __import__(
                            "datetime"
                        ).datetime.fromisoformat(
                            issued_at
                        )
                    )

                    if timestamp.tzinfo is None:

                        timestamp = timestamp.replace(
                            tzinfo=__import__(
                                "datetime"
                            ).timezone.utc
                        )

                except Exception:

                    timestamp = None

            if (
                timestamp
                and (
                    newest_time is None
                    or timestamp > newest_time
                )
            ):

                newest_time = timestamp

        # Old warnings from before this version
        # don't have issued_at, so don't automatically
        # delete them.
        if newest_time is None:
            continue

        age = now - newest_time

        if age >= timedelta(
            days=WARNING_EXPIRE_DAYS
        ):

            member = guild.get_member(
                member_id
            )

            if member is None:
                continue

            members_to_clear.append(
                member
            )

    for member in members_to_clear:

        try:

            count = await clear_all_warnings(
                member,
                "All warnings cleared automatically after 30 days without a new warning.",
                bot.user
            )

            if count:

                print(
                    f"🟢 Automatically cleared "
                    f"{count} warning(s) from "
                    f"{member} after 30 days."
                )

        except Exception as e:

            print(
                f"❌ Failed to automatically "
                f"clear warnings for "
                f"{member}: {e}"
            )


@warning_expiry_loop.before_loop
async def warning_expiry_before_loop():

    await bot.wait_until_ready()


# ================================================================
# ISSUE PUNISHMENT
# ================================================================

async def issue_punishment(
    interaction: discord.Interaction,
    member: discord.Member,
    ptype: str,
    reason: str
):

    global case_counter

    warning_count = 0
    warning_percent = None

    # ============================================================
    # WARNING
    # ============================================================

    if ptype == "WARNING":

        # Count ONLY active warnings.

        warning_count = (
            active_warning_count(
                member.id
            ) + 1
        )

        warning_percent = warning_percent(
            warning_count
        )

        # ========================================================
        # UPDATE ROLES
        # ========================================================

        await update_warning_roles(
            member,
            warning_count,
            f"Warning level {warning_count}"
        )

    # ============================================================
    # CASE NUMBER
    # ============================================================

    case_counter += 1

    case_no = case_counter

    now = discord.utils.utcnow()

    date_text = now.strftime(
        "%d/%m/%Y"
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

        "warning_active": (
            True
            if ptype == "WARNING"
            else False
        ),

        "issued_at": (
            now.isoformat()
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

    # ============================================================
    # RENDER + POST
    # ============================================================

    try:

        posted = await post_card(
            interaction,
            member,
            ptype,
            reason,
            case_no,
            warning_percent,
            log_channel
        )

        if not posted:

            return

    except Exception as e:

        print(
            f"❌ Failed to create punishment card: {e}"
        )

        await interaction.followup.send(
            f"⚠️ {bold('Something went wrong generating the punishment card.')}",
            ephemeral=True
        )

        return

    # ============================================================
    # 3RD WARNING = BAN
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
                    interaction.user
                )
            )

            # Remove warning roles after the ban.

            await update_warning_roles(
                member,
                0,
                "Member reached 3 warnings and was banned."
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
                ephemeral=True
            )

        except discord.Forbidden:

            print(
                f"❌ Could not ban {member}. "
                f"Check bot role position and "
                f"Ban Members permission."
            )

            await interaction.followup.send(
                f"⚠️ {bold('3 warnings reached, but I could not ban the member. Check my role position and Ban Members permission.')}",
                ephemeral=True
            )

        except discord.HTTPException as e:

            print(
                f"❌ Automatic ban failed: {e}"
            )

            await interaction.followup.send(
                f"⚠️ {bold('3 warnings reached, but the automatic ban failed.')}",
                ephemeral=True
            )

        return

    # ============================================================
    # NORMAL WARNING RESPONSE
    # ============================================================

    if ptype == "WARNING":

        await interaction.followup.send(
            f"⚠️ "
            f"{bold(f'Warning {warning_count}/3 logged for')} "
            f"{member.mention} "
            f"{bold(f'— {warning_percent}% severity.')}",
            ephemeral=True
        )

    else:

        await interaction.followup.send(
            f"✅ "
            f"{bold(f'{ptype.title()} logged for')} "
            f"{member.mention} "
            f"{bold('in')} "
            f"{log_channel.mention}.",
            ephemeral=True
        )


# ================================================================
# REJECT
# ================================================================

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
    warning_expiry_loop.start()


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

    print(
        f"⚠️ Warning expiration: "
        f"{WARNING_EXPIRE_DAYS} days"
    )

    print(
        f"🟢 Warning cleared channel: "
        f"{WARNING_CLEARED_CHANNEL_ID}"
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
# /UNWARN
# ================================================================

@bot.tree.command(
    name="unwarn",
    description="Remove the member's latest active warning.",
    guild=GUILD
)
@app_commands.describe(
    member="The member whose warning you want to remove",
    reason="Why the warning is being removed"
)
@app_commands.guild_only()
@mod_only("manage_messages")
async def unwarn_cmd(
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

    # Get active warnings, newest first.

    warnings = get_active_warnings(
        member.id
    )

    if not warnings:

        return await interaction.followup.send(
            f"ℹ️ {bold('That member has no active warnings.')}",
            ephemeral=True
        )

    # Latest warning = highest case number.

    case_no, record = warnings[-1]

    record["warning_active"] = False

    record["cleared_at"] = (
        discord.utils.utcnow().isoformat()
    )

    record["cleared_reason"] = reason

    save_db()

    # Count remaining warnings.

    remaining = active_warning_count(
        member.id
    )

    # Update Warn 1 / Warn 2 roles.

    await update_warning_roles(
        member,
        remaining,
        f"Warning removed manually: {reason}"
    )

    # Send green WARNING CLEARED card.

    await create_warning_cleared_card(
        member,
        1,
        (
            f"{reason} "
            f"— Warning ELT-{case_no:04d} "
            f"was manually removed by "
            f"{interaction.user}."
        ),
        interaction.user
    )

    # Tell the staff member.

    if remaining == 0:

        level_text = "0 active warnings"

    elif remaining == 1:

        level_text = "1 active warning — Warn 1"

    else:

        level_text = (
            f"{remaining} active warnings — Warn 2"
        )

    await interaction.followup.send(
        f"🟢 "
        f"{bold('Warning removed from')} "
        f"{member.mention}. "
        f"{bold(level_text)}.",
        ephemeral=True
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
