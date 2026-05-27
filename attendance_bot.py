import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

load_dotenv()

TOKEN = os.getenv("ATTENDANCE_BOT_TOKEN")
TIMEZONE = ZoneInfo("Europe/Prague")
DB_PATH = os.getenv("ATTENDANCE_DB_PATH", "attendance.db")

intents = discord.Intents.default()
# No privileged intents needed.
intents.members = False

bot = commands.Bot(command_prefix="!", intents=intents)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()


DEFAULT_TEAMS = [
    ("Ferrari", "Fernando Alonso", "Felipe Massa"),
    ("McLaren", "Lewis Hamilton", "Jenson Button"),
    ("Williams", "Rubens Barrichello", "Pastor Maldonado"),
    ("Sauber", "Kamui Kobayashi", "Sergio Perez"),
    ("Red Bull", "Sebastian Vettel", "Mark Webber"),
    ("Mercedes", "Michael Schumacher", "Nico Rosberg"),
    ("Lotus Renault", "Nick Heidfeld", "Vitaly Petrov"),
    ("Force India", "Adrian Sutil", "Paul di Resta"),
    ("Toro Rosso", "Sebastien Buemi", "Jaime Alguersuari"),
    ("Lotus", "Heikki Kovalainen", "Jarno Trulli"),
    ("Virgin", "Timo Glock", "Jerome dAmbrosio"),
    ("HRT", "Vitantonio Liuzzi", "Narain Karthikeyan"),
]



# -------------------------
# DATABASE
# -------------------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT UNIQUE,
    seat1_name TEXT,
    seat1_driver_id INTEGER,
    seat1_driver_name TEXT,
    seat2_name TEXT,
    seat2_driver_id INTEGER,
    seat2_driver_name TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS attendance_events (
    id INTEGER PRIMARY KEY,
    race_name TEXT,
    race_ts INTEGER,
    reserve_ts INTEGER,
    channel_id INTEGER DEFAULT 0,
    message_id INTEGER DEFAULT 0,
    is_open INTEGER DEFAULT 1,
    reserves_assigned INTEGER DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS attendance_votes (
    user_id INTEGER PRIMARY KEY,
    user_name TEXT,
    status TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS reserve_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reserve_id INTEGER,
    reserve_name TEXT,
    team_name TEXT,
    seat_name TEXT,
    replacing_id INTEGER,
    replacing_name TEXT,
    created_ts INTEGER
)
""")

conn.commit()

# Migration from older version
for column in [
    "seat1_name TEXT",
    "seat1_driver_id INTEGER",
    "seat1_driver_name TEXT",
    "seat2_name TEXT",
    "seat2_driver_id INTEGER",
    "seat2_driver_name TEXT",
]:
    try:
        cursor.execute(f"ALTER TABLE teams ADD COLUMN {column}")
        conn.commit()
    except sqlite3.OperationalError:
        pass

try:
    cursor.execute("ALTER TABLE reserve_assignments ADD COLUMN seat_name TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass


# -------------------------
# HELPERS
# -------------------------

def is_admin(interaction: discord.Interaction):
    return interaction.user.guild_permissions.administrator


def now_ts():
    return int(time.time())


def parse_race_datetime(date_text: str, time_text: str):
    """
    date: 23.5.2026 / 23.05.2026 / 23.5.
    time: 20:00
    """
    date_text = date_text.strip()
    time_text = time_text.strip()

    parts = date_text.replace("/", ".").split(".")
    parts = [p for p in parts if p]

    if len(parts) < 2:
        raise ValueError("Invalid date format.")

    day = int(parts[0])
    month = int(parts[1])
    year = int(parts[2]) if len(parts) >= 3 else datetime.now(TIMEZONE).year

    hour, minute = map(int, time_text.split(":"))

    dt = datetime(year, month, day, hour, minute, tzinfo=TIMEZONE)
    return int(dt.timestamp())


def format_dt(ts: int):
    return f"<t:{ts}:F>"


def format_left(ts: int):
    remaining = ts - now_ts()

    if remaining <= 0:
        return "00h 00m"

    hours = remaining // 3600
    minutes = (remaining % 3600) // 60

    return f"{hours:02d}h {minutes:02d}m"


def get_event():
    cursor.execute("""
        SELECT race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned
        FROM attendance_events
        WHERE id = 1
    """)
    return cursor.fetchone()


def set_event(race_name, race_ts, reserve_ts, channel_id=0, message_id=0, is_open=1, reserves_assigned=0):
    cursor.execute("""
        INSERT INTO attendance_events
        (id, race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            race_name = excluded.race_name,
            race_ts = excluded.race_ts,
            reserve_ts = excluded.reserve_ts,
            channel_id = excluded.channel_id,
            message_id = excluded.message_id,
            is_open = excluded.is_open,
            reserves_assigned = excluded.reserves_assigned
    """, (race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned))
    conn.commit()


def get_vote(user_id: int):
    cursor.execute("SELECT status FROM attendance_votes WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None


def set_vote(user: discord.Member | discord.User, status: str):
    cursor.execute("""
        INSERT INTO attendance_votes (user_id, user_name, status)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            user_name = excluded.user_name,
            status = excluded.status
    """, (user.id, user.display_name, status))
    conn.commit()


def status_emoji(status: str):
    return {
        "yes": "✅",
        "no": "❌",
        "maybe": "❓",
        "reserve": "🟡"
    }.get(status, "❔")


def status_name(status: str):
    return {
        "yes": "Racing",
        "no": "Not Racing",
        "maybe": "Maybe",
        "reserve": "Reserve"
    }.get(status, "Unknown")


def get_status_users(status: str):
    cursor.execute("""
        SELECT user_id, user_name
        FROM attendance_votes
        WHERE status = ?
        ORDER BY user_name ASC
    """, (status,))
    return cursor.fetchall()


def format_user_list(rows):
    if not rows:
        return "Nobody"

    text = ""
    for index, (user_id, user_name) in enumerate(rows, start=1):
        text += f"**{index}.** <@{user_id}>\n"

    return text[:1024]


def seat_value(driver_id, driver_name):
    if driver_id:
        return f"<@{driver_id}>"
    return "`EMPTY`"


def find_team(team: str):
    cursor.execute("""
        SELECT team_name, seat1_name, seat1_driver_id, seat1_driver_name, seat2_name, seat2_driver_id, seat2_driver_name
        FROM teams
        WHERE LOWER(team_name) = LOWER(?)
    """, (team,))
    return cursor.fetchone()


def find_seat(team: str, seat: str):
    row = find_team(team)
    if not row:
        return None

    team_name, seat1_name, seat1_driver_id, seat1_driver_name, seat2_name, seat2_driver_id, seat2_driver_name = row
    seat_clean = seat.strip().lower()

    if seat1_name and seat1_name.strip().lower() == seat_clean:
        return {
            "team_name": team_name,
            "slot": 1,
            "seat_name": seat1_name,
            "driver_id": seat1_driver_id,
            "driver_name": seat1_driver_name
        }

    if seat2_name and seat2_name.strip().lower() == seat_clean:
        return {
            "team_name": team_name,
            "slot": 2,
            "seat_name": seat2_name,
            "driver_id": seat2_driver_id,
            "driver_name": seat2_driver_name
        }

    return None


def set_seat_driver(team_name: str, slot: int, member: discord.Member | None):
    if slot == 1:
        if member:
            cursor.execute("""
                UPDATE teams
                SET seat1_driver_id = ?, seat1_driver_name = ?
                WHERE LOWER(team_name) = LOWER(?)
            """, (member.id, member.display_name, team_name))
        else:
            cursor.execute("""
                UPDATE teams
                SET seat1_driver_id = NULL, seat1_driver_name = NULL
                WHERE LOWER(team_name) = LOWER(?)
            """, (team_name,))
    elif slot == 2:
        if member:
            cursor.execute("""
                UPDATE teams
                SET seat2_driver_id = ?, seat2_driver_name = ?
                WHERE LOWER(team_name) = LOWER(?)
            """, (member.id, member.display_name, team_name))
        else:
            cursor.execute("""
                UPDATE teams
                SET seat2_driver_id = NULL, seat2_driver_name = NULL
                WHERE LOWER(team_name) = LOWER(?)
            """, (team_name,))
    conn.commit()


def get_team_slots_for_reserves():
    """
    Returns teams with slots usable by reserves.

    Priority logic:
    1) completely free/problem team with 2 usable slots
    2) teams with 1 usable slot
    3) maybe/no response are lower priority than EMPTY / Not Racing

    A seat is usable when:
    - EMPTY
    - assigned driver voted Not Racing
    - assigned driver voted Maybe
    - assigned driver did not respond

    A seat is NOT usable when assigned driver voted Racing.
    """
    cursor.execute("""
        SELECT team_name, seat1_name, seat1_driver_id, seat1_driver_name, seat2_name, seat2_driver_id, seat2_driver_name
        FROM teams
        ORDER BY team_name ASC
    """)
    rows = cursor.fetchall()

    teams = []

    reason_priority = {
        "empty": 0,
        "no": 1,
        "maybe": 2,
        "no_response": 3
    }

    for team_name, s1, d1_id, d1_name, s2, d2_id, d2_name in rows:
        usable_slots = []
        racing_count = 0

        for slot, seat_name, driver_id, driver_name in [
            (1, s1, d1_id, d1_name),
            (2, s2, d2_id, d2_name)
        ]:
            if not seat_name:
                continue

            if not driver_id:
                usable_slots.append({
                    "team_name": team_name,
                    "slot": slot,
                    "seat_name": seat_name,
                    "driver_id": None,
                    "driver_name": None,
                    "reason": "empty",
                    "reason_priority": reason_priority["empty"]
                })
                continue

            vote = get_vote(driver_id)

            if vote == "yes":
                racing_count += 1
                continue

            reason = vote if vote in ["no", "maybe"] else "no_response"

            usable_slots.append({
                "team_name": team_name,
                "slot": slot,
                "seat_name": seat_name,
                "driver_id": driver_id,
                "driver_name": driver_name,
                "reason": reason,
                "reason_priority": reason_priority.get(reason, 3)
            })

        if usable_slots:
            usable_slots.sort(key=lambda x: x["reason_priority"])

            # Higher priority number = earlier in sorting below.
            # 2 usable slots means bot will try to keep reserves together in one team.
            complete_free_score = 1 if len(usable_slots) >= 2 and racing_count == 0 else 0

            teams.append({
                "team_name": team_name,
                "usable_slots": usable_slots,
                "complete_free_score": complete_free_score,
                "usable_count": len(usable_slots),
                "best_reason_priority": min(slot["reason_priority"] for slot in usable_slots)
            })

    teams.sort(
        key=lambda t: (
            -t["complete_free_score"],   # full/free teams first
            -t["usable_count"],          # then teams with more available slots
            t["best_reason_priority"],   # then EMPTY / Not Racing before Maybe / no response
            t["team_name"].lower()
        )
    )

    return teams


def missing_or_empty_slots():
    """
    Backwards-compatible flat list used by older code.
    """
    flat = []
    for team in get_team_slots_for_reserves():
        for slot in team["usable_slots"]:
            flat.append((
                slot["team_name"],
                slot["slot"],
                slot["seat_name"],
                slot["driver_id"],
                slot["driver_name"],
                slot["reason"]
            ))
    return flat



def get_team_choices(current: str = ""):
    current = (current or "").lower().strip()

    try:
        cursor.execute("""
            SELECT team_name
            FROM teams
            ORDER BY team_name ASC
        """)
        teams = [row[0] for row in cursor.fetchall()]
    except Exception as e:
        print(f"Team autocomplete DB error: {e}")
        teams = []

    if not teams:
        teams = [team for team, seat1, seat2 in DEFAULT_TEAMS]

    choices = []
    for team_name in teams:
        if current in team_name.lower():
            choices.append(app_commands.Choice(name=team_name[:100], value=team_name[:100]))

    return choices[:25]


def get_all_seat_choices(current: str = ""):
    current = (current or "").lower().strip()
    choices = []

    try:
        cursor.execute("""
            SELECT team_name, seat1_name, seat1_driver_id, seat2_name, seat2_driver_id
            FROM teams
            ORDER BY team_name ASC
        """)
        rows = cursor.fetchall()
    except Exception as e:
        print(f"Seat autocomplete DB error: {e}")
        rows = []

    if not rows:
        for team_name, seat1, seat2 in DEFAULT_TEAMS:
            for seat_name in [seat1, seat2]:
                label = f"{team_name} — {seat_name} — EMPTY"
                if current in team_name.lower() or current in seat_name.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=seat_name[:100]))
        return choices[:25]

    for team_name, seat1_name, seat1_driver_id, seat2_name, seat2_driver_id in rows:
        for seat_name, driver_id in [(seat1_name, seat1_driver_id), (seat2_name, seat2_driver_id)]:
            if not seat_name:
                continue
            status = "occupied" if driver_id else "EMPTY"
            label = f"{team_name} — {seat_name} — {status}"
            if current in team_name.lower() or current in seat_name.lower():
                choices.append(app_commands.Choice(name=label[:100], value=seat_name[:100]))

    return choices[:25]


def get_seat_choices_for_team(team: str | None, current: str = ""):
    current = (current or "").lower().strip()

    if not team:
        return get_all_seat_choices(current)

    row = find_team(str(team))

    if not row:
        for default_team, seat1, seat2 in DEFAULT_TEAMS:
            if default_team.lower() == str(team).lower():
                choices = []
                for seat_name in [seat1, seat2]:
                    label = f"{seat_name} — EMPTY"
                    if current in seat_name.lower():
                        choices.append(app_commands.Choice(name=label[:100], value=seat_name[:100]))
                return choices[:25]
        return get_all_seat_choices(current)

    team_name, seat1_name, seat1_driver_id, seat1_driver_name, seat2_name, seat2_driver_id, seat2_driver_name = row
    choices = []

    for seat_name, driver_id in [(seat1_name, seat1_driver_id), (seat2_name, seat2_driver_id)]:
        if not seat_name:
            continue
        status = "occupied" if driver_id else "EMPTY"
        label = f"{seat_name} — {status}"
        if current in seat_name.lower():
            choices.append(app_commands.Choice(name=label[:100], value=seat_name[:100]))

    if not choices and current == "":
        return get_all_seat_choices(current)

    return choices[:25]


async def team_autocomplete(interaction: discord.Interaction, current: str):
    try:
        return get_team_choices(current)
    except Exception as e:
        print(f"team_autocomplete error: {e}")
        return []


async def seat_autocomplete(interaction: discord.Interaction, current: str):
    try:
        selected_team = getattr(interaction.namespace, "team", None)

        if hasattr(selected_team, "value"):
            selected_team = selected_team.value

        return get_seat_choices_for_team(selected_team, current)
    except Exception as e:
        print(f"seat_autocomplete error: {e}")
        return []


def create_attendance_embed():
    event = get_event()

    if not event:
        return discord.Embed(
            title="🏁 CSL ATTENDANCE",
            description="No attendance check has been created.",
            color=discord.Color.red()
        )

    race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned = event
    status = "🟢 OPEN" if is_open == 1 else "🔴 CLOSED"

    embed = discord.Embed(
        title=f"🏁 CSL ATTENDANCE — {race_name}",
        description="Click the button based on your race availability.",
        color=discord.Color.green() if is_open == 1 else discord.Color.red()
    )

    embed.add_field(name="📅 Race Start", value=f"{format_dt(race_ts)}\n**In {format_left(race_ts)}**", inline=True)
    embed.add_field(name="🟡 Reserve Check", value=f"{format_dt(reserve_ts)}\n**In {format_left(reserve_ts)}**", inline=True)
    embed.add_field(name="📊 Status", value=f"**{status}**", inline=True)

    embed.add_field(name="✅ Racing", value=format_user_list(get_status_users("yes")), inline=True)
    embed.add_field(name="❌ Not Racing", value=format_user_list(get_status_users("no")), inline=True)
    embed.add_field(name="❓ Maybe", value=format_user_list(get_status_users("maybe")), inline=True)
    embed.add_field(name="🟡 Reserves", value=format_user_list(get_status_users("reserve")), inline=False)

    embed.set_footer(text="CSL Attendance System • FIA Reserve Manager")
    return embed


def create_teams_embed():
    cursor.execute("""
        SELECT team_name, seat1_name, seat1_driver_id, seat2_name, seat2_driver_id
        FROM teams
        ORDER BY team_name ASC
    """)
    rows = cursor.fetchall()

    embed = discord.Embed(
        title="🏎️ CSL TEAM DATABASE",
        color=discord.Color.blurple()
    )

    if not rows:
        embed.description = "No teams have been saved yet."
        return embed

    for team_name, seat1_name, seat1_driver_id, seat2_name, seat2_driver_id in rows:
        value = ""

        if seat1_name:
            value += f"• **{seat1_name}** → {seat_value(seat1_driver_id, None)}\n"

        if seat2_name:
            value += f"• **{seat2_name}** → {seat_value(seat2_driver_id, None)}\n"

        if not value:
            value = "`No seats configured`"

        embed.add_field(name=f"🏁 {team_name}", value=value, inline=False)

    embed.set_footer(text="CSL Attendance System • FIA Team Manager")
    return embed


class AttendanceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def handle_vote(self, interaction: discord.Interaction, status: str):
        event = get_event()

        if not event:
            await interaction.response.send_message("❌ No attendance check is active.", ephemeral=True)
            return

        race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned = event

        if is_open != 1:
            await interaction.response.send_message("❌ Attendance check is closed.", ephemeral=True)
            return

        set_vote(interaction.user, status)

        await interaction.response.send_message(
            f"{status_emoji(status)} Your status has been set to **{status_name(status)}**.",
            ephemeral=True
        )

        await update_attendance_message()

    @discord.ui.button(label="Racing", emoji="✅", style=discord.ButtonStyle.green, custom_id="attendance_yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "yes")

    @discord.ui.button(label="Not Racing", emoji="❌", style=discord.ButtonStyle.red, custom_id="attendance_no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "no")

    @discord.ui.button(label="Maybe", emoji="❓", style=discord.ButtonStyle.gray, custom_id="attendance_maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "maybe")

    @discord.ui.button(label="Reserve", emoji="🟡", style=discord.ButtonStyle.blurple, custom_id="attendance_reserve")
    async def reserve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "reserve")


async def update_attendance_message():
    event = get_event()

    if not event:
        return

    race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned = event

    if not channel_id or not message_id:
        return

    channel = bot.get_channel(channel_id)

    if not channel:
        return

    try:
        msg = await channel.fetch_message(message_id)
        await msg.edit(embed=create_attendance_embed(), view=AttendanceView())
    except Exception as e:
        print(f"Attendance update error: {e}")


async def auto_assign_reserves(channel: discord.TextChannel | discord.Thread, manual=False):
    event = get_event()

    if not event:
        return "❌ No attendance check is active."

    race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned = event

    if reserves_assigned == 1 and not manual:
        return "ℹ️ Reserve assignments have already been created."

    cursor.execute("""
        SELECT user_id, user_name
        FROM attendance_votes
        WHERE status = 'reserve'
        ORDER BY user_name ASC
    """)
    reserves = cursor.fetchall()

    if not reserves:
        return "ℹ️ No reserve drivers are available."

    team_slots = get_team_slots_for_reserves()

    if not team_slots:
        return "✅ No reserve assignment needed. All active seats are covered."

    assignments = []
    used_reserves = set()
    reserve_index = 0

    # Smart assignment:
    # - process full/free teams first
    # - when a team has 2 usable seats and at least 2 reserves, fill both seats together
    for team in team_slots:
        for slot_data in team["usable_slots"]:
            while reserve_index < len(reserves) and reserves[reserve_index][0] in used_reserves:
                reserve_index += 1

            if reserve_index >= len(reserves):
                break

            reserve_id, reserve_name = reserves[reserve_index]
            reserve_index += 1
            used_reserves.add(reserve_id)

            cursor.execute("""
                INSERT INTO reserve_assignments
                (reserve_id, reserve_name, team_name, seat_name, replacing_id, replacing_name, created_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                reserve_id,
                reserve_name,
                slot_data["team_name"],
                slot_data["seat_name"],
                slot_data["driver_id"],
                slot_data["driver_name"],
                now_ts()
            ))
            conn.commit()

            assignments.append({
                "reserve_id": reserve_id,
                "reserve_name": reserve_name,
                "team_name": slot_data["team_name"],
                "seat_name": slot_data["seat_name"],
                "replacing_id": slot_data["driver_id"],
                "reason": slot_data["reason"]
            })

        if reserve_index >= len(reserves):
            break

    if not assignments:
        return "ℹ️ No reserve assignments were created."

    cursor.execute("UPDATE attendance_events SET reserves_assigned = 1 WHERE id = 1")
    conn.commit()

    embed = discord.Embed(
        title="🟡 AUTO RESERVE ASSIGNMENTS",
        description=f"Smart reserve assignment for **{race_name}**.",
        color=discord.Color.orange()
    )

    grouped = {}
    for item in assignments:
        grouped.setdefault(item["team_name"], []).append(item)

    text = ""
    for team_name, items in grouped.items():
        text += f"**{team_name}**\n"
        for item in items:
            text += f"• <@{item['reserve_id']}> → **{item['seat_name']}**"
            if item["replacing_id"]:
                text += f" replacing <@{item['replacing_id']}>"
            text += "\n"
        text += "\n"

    embed.add_field(name="Assignments", value=text[:1024], inline=False)
    embed.add_field(name="Race Start", value=format_dt(race_ts), inline=True)

    remaining_reserves = len(reserves) - len(assignments)
    if remaining_reserves > 0:
        embed.add_field(name="Unused Reserves", value=f"**{remaining_reserves}** reserve(s) were not needed.", inline=True)

    embed.set_footer(text="CSL Attendance System • Smart Auto Reserve Manager")

    await channel.send(embed=embed)
    return f"✅ Auto reserve assignments created: **{len(assignments)}**"



@tasks.loop(seconds=60)
async def reserve_checker():
    event = get_event()

    if not event:
        return

    race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned = event

    if reserves_assigned == 1:
        return

    if reserve_ts != 0 and now_ts() >= reserve_ts:
        channel = bot.get_channel(channel_id)

        if channel:
            await auto_assign_reserves(channel)


@bot.event
async def on_ready():
    bot.add_view(AttendanceView())
    synced = await bot.tree.sync()
    print(f"Attendance bot online as {bot.user}")
    print(f"Synced {len(synced)} commands")

    if not reserve_checker.is_running():
        reserve_checker.start()


# -------------------------
# TEAM COMMANDS
# -------------------------

@bot.tree.command(name="team_edit", description="Admin: create or edit a team with IRL seats")
@app_commands.describe(
    team="Team name",
    seat1_name="IRL driver / seat 1, e.g. Michael Schumacher",
    seat1_driver="Discord driver for seat 1, optional",
    seat2_name="IRL driver / seat 2, e.g. Nico Rosberg",
    seat2_driver="Discord driver for seat 2, optional"
)
async def team_edit(
    interaction: discord.Interaction,
    team: str,
    seat1_name: str,
    seat1_driver: discord.Member = None,
    seat2_name: str = None,
    seat2_driver: discord.Member = None
):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    seat1_driver_id = seat1_driver.id if seat1_driver else None
    seat1_driver_name = seat1_driver.display_name if seat1_driver else None
    seat2_driver_id = seat2_driver.id if seat2_driver else None
    seat2_driver_name = seat2_driver.display_name if seat2_driver else None

    cursor.execute("""
        INSERT INTO teams
        (team_name, seat1_name, seat1_driver_id, seat1_driver_name, seat2_name, seat2_driver_id, seat2_driver_name)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_name) DO UPDATE SET
            seat1_name = excluded.seat1_name,
            seat1_driver_id = excluded.seat1_driver_id,
            seat1_driver_name = excluded.seat1_driver_name,
            seat2_name = excluded.seat2_name,
            seat2_driver_id = excluded.seat2_driver_id,
            seat2_driver_name = excluded.seat2_driver_name
    """, (
        team,
        seat1_name,
        seat1_driver_id,
        seat1_driver_name,
        seat2_name,
        seat2_driver_id,
        seat2_driver_name
    ))
    conn.commit()

    embed = discord.Embed(
        title="✅ Team saved",
        color=discord.Color.green()
    )
    embed.add_field(name=f"🏁 {team}", value=(
        f"• **{seat1_name}** → {seat_value(seat1_driver_id, seat1_driver_name)}\n"
        f"• **{seat2_name or 'Seat 2'}** → {seat_value(seat2_driver_id, seat2_driver_name)}"
    ), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="team_add_2011_defaults", description="Admin: add default 2011 team seats as EMPTY")
@app_commands.describe(confirm="Type CONFIRM")
async def team_add_2011_defaults(interaction: discord.Interaction, confirm: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    if confirm != "CONFIRM":
        await interaction.response.send_message("❌ Type exactly `CONFIRM`.", ephemeral=True)
        return

    for team, seat1, seat2 in DEFAULT_TEAMS:
        cursor.execute("""
            INSERT INTO teams
            (team_name, seat1_name, seat1_driver_id, seat1_driver_name, seat2_name, seat2_driver_id, seat2_driver_name)
            VALUES (?, ?, NULL, NULL, ?, NULL, NULL)
            ON CONFLICT(team_name) DO UPDATE SET
                seat1_name = excluded.seat1_name,
                seat2_name = excluded.seat2_name
        """, (team, seat1, seat2))

    conn.commit()

    await interaction.response.send_message("✅ Default 2011 team seats have been added as EMPTY.", ephemeral=True)


@bot.tree.command(name="team_set_driver", description="Admin: set Discord driver for a specific team seat")
@app_commands.describe(
    team="Team name",
    seat="IRL driver / seat name",
    driver="Discord driver"
)
async def team_set_driver(interaction: discord.Interaction, team: str, seat: str, driver: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    seat_data = find_seat(team, seat)

    if not seat_data:
        await interaction.response.send_message("❌ Team or seat not found.", ephemeral=True)
        return

    set_seat_driver(seat_data["team_name"], seat_data["slot"], driver)

    await interaction.response.send_message(
        f"✅ **{driver.display_name}** assigned to **{seat_data['team_name']}** as **{seat_data['seat_name']}**.",
        ephemeral=True
    )


@bot.tree.command(name="team_remove_driver", description="Admin: remove Discord driver from a specific seat")
@app_commands.describe(team="Team name", seat="IRL driver / seat name")
async def team_remove_driver(interaction: discord.Interaction, team: str, seat: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    seat_data = find_seat(team, seat)

    if not seat_data:
        await interaction.response.send_message("❌ Team or seat not found.", ephemeral=True)
        return

    old_driver_id = seat_data["driver_id"]
    set_seat_driver(seat_data["team_name"], seat_data["slot"], None)

    if old_driver_id:
        text = f"✅ <@{old_driver_id}> removed from **{seat_data['team_name']}** as **{seat_data['seat_name']}**."
    else:
        text = f"ℹ️ Seat **{seat_data['seat_name']}** in **{seat_data['team_name']}** was already EMPTY."

    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(name="team_remove", description="Admin: delete a full team")
@app_commands.describe(team="Team name")
async def team_remove(interaction: discord.Interaction, team: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    cursor.execute("DELETE FROM teams WHERE LOWER(team_name) = LOWER(?)", (team,))
    conn.commit()

    await interaction.response.send_message(f"🗑️ Team **{team}** has been deleted.", ephemeral=True)


@bot.tree.command(name="team_clear", description="Admin: delete all teams")
@app_commands.describe(confirm="Type RESET")
async def team_clear(interaction: discord.Interaction, confirm: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    if confirm != "RESET":
        await interaction.response.send_message("❌ Type exactly `RESET`.", ephemeral=True)
        return

    cursor.execute("DELETE FROM teams")
    conn.commit()

    await interaction.response.send_message("🗑️ All teams have been deleted.", ephemeral=True)


@bot.tree.command(name="teams", description="Admin: show team database")
async def teams(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    await interaction.response.send_message(embed=create_teams_embed(), ephemeral=True)


# -------------------------
# ATTENDANCE COMMANDS
# -------------------------

@bot.tree.command(name="attendance_create", description="Admin: create attendance check")
@app_commands.describe(
    race="Race name, e.g. Race 16 - Surfers Paradise 🇦🇺",
    date="Date, e.g. 23.5.2026",
    time_text="Race time, e.g. 20:00",
    reserve_minutes_before="When reserve assignment happens, e.g. 60"
)
async def attendance_create(
    interaction: discord.Interaction,
    race: str,
    date: str,
    time_text: str,
    reserve_minutes_before: int = 60
):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    try:
        race_ts = parse_race_datetime(date, time_text)
    except Exception:
        await interaction.response.send_message("❌ Invalid date or time format. Use `23.5.2026` and `20:00`.", ephemeral=True)
        return

    reserve_ts = race_ts - reserve_minutes_before * 60

    cursor.execute("DELETE FROM attendance_votes")
    cursor.execute("DELETE FROM reserve_assignments")
    conn.commit()

    set_event(race, race_ts, reserve_ts, interaction.channel.id, 0, 1, 0)

    await interaction.response.send_message(embed=create_attendance_embed(), view=AttendanceView())
    msg = await interaction.original_response()

    set_event(race, race_ts, reserve_ts, msg.channel.id, msg.id, 1, 0)


@bot.tree.command(name="attendance_status", description="Show attendance status")
async def attendance_status(interaction: discord.Interaction):
    await interaction.response.send_message(embed=create_attendance_embed())


@bot.tree.command(name="attendance_close", description="Admin: close attendance check")
async def attendance_close(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    event = get_event()

    if not event:
        await interaction.response.send_message("❌ No attendance check is active.", ephemeral=True)
        return

    race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned = event

    set_event(race_name, race_ts, reserve_ts, channel_id, message_id, 0, reserves_assigned)
    await update_attendance_message()

    await interaction.response.send_message("🔒 Attendance check has been closed.", ephemeral=True)


@bot.tree.command(name="attendance_reset", description="Admin: reset attendance votes")
@app_commands.describe(confirm="Type RESET")
async def attendance_reset(interaction: discord.Interaction, confirm: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    if confirm != "RESET":
        await interaction.response.send_message("❌ Type exactly `RESET`.", ephemeral=True)
        return

    cursor.execute("DELETE FROM attendance_votes")
    cursor.execute("DELETE FROM reserve_assignments")
    conn.commit()

    event = get_event()

    if event:
        race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned = event
        set_event(race_name, race_ts, reserve_ts, channel_id, message_id, is_open, 0)
        await update_attendance_message()

    await interaction.response.send_message("✅ Attendance votes have been reset.", ephemeral=True)


@bot.tree.command(name="reserves", description="Admin: show active reserves")
async def reserves(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    rows = get_status_users("reserve")

    embed = discord.Embed(
        title="🟡 ACTIVE RESERVES",
        color=discord.Color.orange()
    )

    embed.description = format_user_list(rows)
    embed.set_footer(text="CSL Attendance System • Reserve List")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="reserve_assign", description="Admin: manually assign reserve to team seat")
@app_commands.describe(
    reserve="Reserve driver",
    team="Team name",
    seat="IRL driver / seat name"
)
async def reserve_assign(interaction: discord.Interaction, reserve: discord.Member, team: str, seat: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    vote = get_vote(reserve.id)

    if vote != "reserve":
        await interaction.response.send_message(
            "❌ This driver is not marked as Reserve.",
            ephemeral=True
        )
        return

    seat_data = find_seat(team, seat)

    if not seat_data:
        await interaction.response.send_message("❌ Team or seat not found.", ephemeral=True)
        return

    event = get_event()
    race_name = event[0] if event else "Current Race"
    race_ts = event[1] if event else 0

    replacing_id = seat_data["driver_id"]
    replacing_name = seat_data["driver_name"]

    set_seat_driver(seat_data["team_name"], seat_data["slot"], reserve)

    cursor.execute("""
        INSERT INTO reserve_assignments
        (reserve_id, reserve_name, team_name, seat_name, replacing_id, replacing_name, created_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        reserve.id,
        reserve.display_name,
        seat_data["team_name"],
        seat_data["seat_name"],
        replacing_id,
        replacing_name,
        now_ts()
    ))
    conn.commit()

    embed = discord.Embed(
        title="🟡 RESERVE ASSIGNMENT",
        description=f"{reserve.mention} has been assigned to **{seat_data['team_name']}** as **{seat_data['seat_name']}**.",
        color=discord.Color.orange()
    )

    if replacing_id:
        embed.add_field(name="Replacing", value=f"<@{replacing_id}>", inline=True)

    embed.add_field(name="Race", value=f"**{race_name}**", inline=True)

    if race_ts:
        embed.add_field(name="Race Start", value=format_dt(race_ts), inline=True)

    embed.set_footer(text="CSL Attendance System • FIA Reserve Manager")

    await interaction.response.send_message(embed=embed)



# -------------------------
# AUTOCOMPLETE
# -------------------------

@team_set_driver.autocomplete("team")
async def team_set_driver_team_autocomplete(interaction: discord.Interaction, current: str):
    return await team_autocomplete(interaction, current)


@team_set_driver.autocomplete("seat")
async def team_set_driver_seat_autocomplete(interaction: discord.Interaction, current: str):
    return await seat_autocomplete(interaction, current)


@team_remove_driver.autocomplete("team")
async def team_remove_driver_team_autocomplete(interaction: discord.Interaction, current: str):
    return await team_autocomplete(interaction, current)


@team_remove_driver.autocomplete("seat")
async def team_remove_driver_seat_autocomplete(interaction: discord.Interaction, current: str):
    return await seat_autocomplete(interaction, current)


@team_remove.autocomplete("team")
async def team_remove_team_autocomplete(interaction: discord.Interaction, current: str):
    return await team_autocomplete(interaction, current)


@reserve_assign.autocomplete("team")
async def reserve_assign_team_autocomplete(interaction: discord.Interaction, current: str):
    return await team_autocomplete(interaction, current)


@reserve_assign.autocomplete("seat")
async def reserve_assign_seat_autocomplete(interaction: discord.Interaction, current: str):
    return await seat_autocomplete(interaction, current)


@bot.tree.command(name="multi_reserve_assign", description="Admin: assign up to 5 reserves at once")
@app_commands.describe(
    reserve1="Reserve driver 1",
    team1="Team for reserve 1",
    seat1="Seat / IRL driver for reserve 1",
    reserve2="Reserve driver 2",
    team2="Team for reserve 2",
    seat2="Seat / IRL driver for reserve 2",
    reserve3="Reserve driver 3",
    team3="Team for reserve 3",
    seat3="Seat / IRL driver for reserve 3",
    reserve4="Reserve driver 4",
    team4="Team for reserve 4",
    seat4="Seat / IRL driver for reserve 4",
    reserve5="Reserve driver 5",
    team5="Team for reserve 5",
    seat5="Seat / IRL driver for reserve 5"
)
async def multi_reserve_assign(
    interaction: discord.Interaction,
    reserve1: discord.Member,
    team1: str,
    seat1: str,
    reserve2: discord.Member = None,
    team2: str = None,
    seat2: str = None,
    reserve3: discord.Member = None,
    team3: str = None,
    seat3: str = None,
    reserve4: discord.Member = None,
    team4: str = None,
    seat4: str = None,
    reserve5: discord.Member = None,
    team5: str = None,
    seat5: str = None
):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    entries = [
        (reserve1, team1, seat1),
        (reserve2, team2, seat2),
        (reserve3, team3, seat3),
        (reserve4, team4, seat4),
        (reserve5, team5, seat5),
    ]

    cleaned = []

    for reserve, team, seat in entries:
        if reserve is None and not team and not seat:
            continue

        if not (reserve and team and seat):
            await interaction.response.send_message(
                "❌ Every reserve assignment must include reserve, team and seat.",
                ephemeral=True
            )
            return

        cleaned.append((reserve, team, seat))

    if not cleaned:
        await interaction.response.send_message("❌ No reserve assignments provided.", ephemeral=True)
        return

    event = get_event()
    race_name = event[0] if event else "Current Race"
    race_ts = event[1] if event else 0

    assigned = []
    errors = []

    for reserve, team, seat in cleaned:
        vote = get_vote(reserve.id)

        if vote != "reserve":
            errors.append(f"❌ {reserve.mention} is not marked as Reserve.")
            continue

        seat_data = find_seat(team, seat)

        if not seat_data:
            errors.append(f"❌ `{team}` / `{seat}` was not found.")
            continue

        replacing_id = seat_data["driver_id"]
        replacing_name = seat_data["driver_name"]

        set_seat_driver(seat_data["team_name"], seat_data["slot"], reserve)

        cursor.execute("""
            INSERT INTO reserve_assignments
            (reserve_id, reserve_name, team_name, seat_name, replacing_id, replacing_name, created_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            reserve.id,
            reserve.display_name,
            seat_data["team_name"],
            seat_data["seat_name"],
            replacing_id,
            replacing_name,
            now_ts()
        ))
        conn.commit()

        assigned.append((reserve, seat_data["team_name"], seat_data["seat_name"], replacing_id))

    if not assigned and errors:
        await interaction.response.send_message("\n".join(errors)[:1900], ephemeral=True)
        return

    embed = discord.Embed(
        title="🟡 MULTI RESERVE ASSIGNMENTS",
        description=f"Reserve assignments for **{race_name}**.",
        color=discord.Color.orange()
    )

    text = ""
    for reserve, team_name, seat_name, replacing_id in assigned:
        text += f"🟡 {reserve.mention} → **{team_name}** as **{seat_name}**"
        if replacing_id:
            text += f" replacing <@{replacing_id}>"
        text += "\n"

    embed.add_field(name="Assignments", value=text[:1024] if text else "No assignments.", inline=False)

    if race_ts:
        embed.add_field(name="Race Start", value=format_dt(race_ts), inline=True)

    if errors:
        embed.add_field(name="Skipped / Errors", value="\n".join(errors)[:1024], inline=False)

    embed.set_footer(text="CSL Attendance System • FIA Reserve Manager")

    await interaction.response.send_message(embed=embed)


# Autocomplete for multi reserve assign
@multi_reserve_assign.autocomplete("team1")
async def multi_team1_autocomplete(interaction: discord.Interaction, current: str):
    return await team_autocomplete(interaction, current)

@multi_reserve_assign.autocomplete("team2")
async def multi_team2_autocomplete(interaction: discord.Interaction, current: str):
    return await team_autocomplete(interaction, current)

@multi_reserve_assign.autocomplete("team3")
async def multi_team3_autocomplete(interaction: discord.Interaction, current: str):
    return await team_autocomplete(interaction, current)

@multi_reserve_assign.autocomplete("team4")
async def multi_team4_autocomplete(interaction: discord.Interaction, current: str):
    return await team_autocomplete(interaction, current)

@multi_reserve_assign.autocomplete("team5")
async def multi_team5_autocomplete(interaction: discord.Interaction, current: str):
    return await team_autocomplete(interaction, current)


async def multi_seat_autocomplete_for(interaction: discord.Interaction, current: str, team_field: str):
    selected_team = getattr(interaction.namespace, team_field, None)

    if hasattr(selected_team, "value"):
        selected_team = selected_team.value

    return get_seat_choices_for_team(selected_team, current)


@multi_reserve_assign.autocomplete("seat1")
async def multi_seat1_autocomplete(interaction: discord.Interaction, current: str):
    return await multi_seat_autocomplete_for(interaction, current, "team1")

@multi_reserve_assign.autocomplete("seat2")
async def multi_seat2_autocomplete(interaction: discord.Interaction, current: str):
    return await multi_seat_autocomplete_for(interaction, current, "team2")

@multi_reserve_assign.autocomplete("seat3")
async def multi_seat3_autocomplete(interaction: discord.Interaction, current: str):
    return await multi_seat_autocomplete_for(interaction, current, "team3")

@multi_reserve_assign.autocomplete("seat4")
async def multi_seat4_autocomplete(interaction: discord.Interaction, current: str):
    return await multi_seat_autocomplete_for(interaction, current, "team4")

@multi_reserve_assign.autocomplete("seat5")
async def multi_seat5_autocomplete(interaction: discord.Interaction, current: str):
    return await multi_seat_autocomplete_for(interaction, current, "team5")


@bot.tree.command(name="auto_reserve_assign", description="Admin: run smart automatic reserve assignment now")
async def auto_reserve_assign(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    result = await auto_assign_reserves(interaction.channel, manual=True)

    await interaction.followup.send(result, ephemeral=True)


if not TOKEN:
    raise RuntimeError("Missing ATTENDANCE_BOT_TOKEN in environment variables.")

bot.run(TOKEN)
