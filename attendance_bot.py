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
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT UNIQUE,
    driver1_id INTEGER,
    driver1_name TEXT,
    driver2_id INTEGER,
    driver2_name TEXT
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
    replacing_id INTEGER,
    replacing_name TEXT,
    created_ts INTEGER
)
""")

conn.commit()


def is_admin(interaction: discord.Interaction):
    return interaction.user.guild_permissions.administrator


def now_ts():
    return int(time.time())


def parse_race_datetime(date_text: str, time_text: str):
    date_text = date_text.strip()
    time_text = time_text.strip()
    parts = date_text.replace("/", ".").split(".")
    parts = [p for p in parts if p]

    if len(parts) < 2:
        raise ValueError("Bad date")

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
    return {"yes": "✅", "no": "❌", "maybe": "❓", "reserve": "🟡"}.get(status, "❔")


def status_name(status: str):
    return {"yes": "Jedu", "no": "Nejedu", "maybe": "Nevím", "reserve": "Rezerva"}.get(status, "Unknown")


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
        return "Nikdo"
    text = ""
    for index, (user_id, user_name) in enumerate(rows, start=1):
        text += f"**{index}.** <@{user_id}>\n"
    return text[:1024]


def team_missing_slots():
    cursor.execute("""
        SELECT team_name, driver1_id, driver1_name, driver2_id, driver2_name
        FROM teams
        ORDER BY team_name ASC
    """)
    teams = cursor.fetchall()
    missing = []

    for team_name, d1_id, d1_name, d2_id, d2_name in teams:
        for driver_id, driver_name in [(d1_id, d1_name), (d2_id, d2_name)]:
            if not driver_id:
                continue
            vote = get_vote(driver_id)
            if vote in [None, "no", "maybe"]:
                missing.append((team_name, driver_id, driver_name))

    return missing


def create_attendance_embed():
    event = get_event()
    if not event:
        return discord.Embed(
            title="🏁 CSL ATTENDANCE",
            description="Žádný attendance check není vytvořený.",
            color=discord.Color.red()
        )

    race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned = event
    status = "🟢 OPEN" if is_open == 1 else "🔴 CLOSED"

    embed = discord.Embed(
        title=f"🏁 CSL ATTENDANCE — {race_name}",
        description="Klikni na tlačítko podle toho, jestli jedeš závod.",
        color=discord.Color.green() if is_open == 1 else discord.Color.red()
    )

    embed.add_field(name="📅 Race start", value=f"{format_dt(race_ts)}\n**Za {format_left(race_ts)}**", inline=True)
    embed.add_field(name="🟡 Reserve check", value=f"{format_dt(reserve_ts)}\n**Za {format_left(reserve_ts)}**", inline=True)
    embed.add_field(name="📊 Status", value=f"**{status}**", inline=True)
    embed.add_field(name="✅ Jedu", value=format_user_list(get_status_users("yes")), inline=True)
    embed.add_field(name="❌ Nejedu", value=format_user_list(get_status_users("no")), inline=True)
    embed.add_field(name="❓ Nevím", value=format_user_list(get_status_users("maybe")), inline=True)
    embed.add_field(name="🟡 Rezerva", value=format_user_list(get_status_users("reserve")), inline=False)
    embed.set_footer(text="CSL Attendance System • FIA Reserve Manager")
    return embed


class AttendanceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def handle_vote(self, interaction: discord.Interaction, status: str):
        event = get_event()
        if not event:
            await interaction.response.send_message("❌ Žádný attendance check není aktivní.", ephemeral=True)
            return

        race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned = event
        if is_open != 1:
            await interaction.response.send_message("❌ Attendance check je uzavřený.", ephemeral=True)
            return

        set_vote(interaction.user, status)
        await interaction.response.send_message(
            f"{status_emoji(status)} Tvoje odpověď byla nastavena na **{status_name(status)}**.",
            ephemeral=True
        )
        await update_attendance_message()

    @discord.ui.button(label="Jedu", emoji="✅", style=discord.ButtonStyle.green, custom_id="attendance_yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "yes")

    @discord.ui.button(label="Nejedu", emoji="❌", style=discord.ButtonStyle.red, custom_id="attendance_no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "no")

    @discord.ui.button(label="Nevím", emoji="❓", style=discord.ButtonStyle.gray, custom_id="attendance_maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_vote(interaction, "maybe")

    @discord.ui.button(label="Rezerva", emoji="🟡", style=discord.ButtonStyle.blurple, custom_id="attendance_reserve")
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


async def assign_reserves(channel: discord.TextChannel | discord.Thread, manual=False):
    event = get_event()
    if not event:
        return "❌ Žádný attendance check není aktivní."

    race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned = event
    if reserves_assigned == 1 and not manual:
        return "ℹ️ Rezervy už byly automaticky přiřazeny."

    cursor.execute("""
        SELECT user_id, user_name
        FROM attendance_votes
        WHERE status = 'reserve'
        ORDER BY user_name ASC
    """)
    reserves = cursor.fetchall()
    missing = team_missing_slots()

    if not reserves:
        return "ℹ️ Nejsou žádní rezervní jezdci."
    if not missing:
        return "✅ Všechny týmy jsou kompletní. Rezervy nejsou potřeba."

    assignments = []
    used_reserves = set()

    for team_name, missing_id, missing_name in missing:
        reserve = None
        for reserve_id, reserve_name in reserves:
            if reserve_id not in used_reserves and reserve_id != missing_id:
                reserve = (reserve_id, reserve_name)
                break
        if not reserve:
            break

        reserve_id, reserve_name = reserve
        used_reserves.add(reserve_id)

        cursor.execute("""
            INSERT INTO reserve_assignments
            (reserve_id, reserve_name, team_name, replacing_id, replacing_name, created_ts)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (reserve_id, reserve_name, team_name, missing_id, missing_name, now_ts()))
        conn.commit()
        assignments.append((reserve_id, reserve_name, team_name, missing_id, missing_name))

    if not assignments:
        return "ℹ️ Nepodařilo se vytvořit žádné přiřazení."

    cursor.execute("UPDATE attendance_events SET reserves_assigned = 1 WHERE id = 1")
    conn.commit()

    embed = discord.Embed(
        title="🟡 CSL RESERVE ASSIGNMENTS",
        description=f"Automatické přiřazení rezerv pro **{race_name}**.",
        color=discord.Color.orange()
    )

    text = ""
    for reserve_id, reserve_name, team_name, missing_id, missing_name in assignments:
        text += f"🟡 <@{reserve_id}> → **{team_name}** místo <@{missing_id}>\n"

    embed.add_field(name="📋 Assignments", value=text[:1024], inline=False)
    embed.add_field(name="🏁 Race", value=f"**{race_name}**", inline=True)
    embed.add_field(name="📅 Start", value=format_dt(race_ts), inline=True)
    embed.set_footer(text="CSL Attendance System • FIA Reserve Manager")

    await channel.send(embed=embed)
    return f"✅ Přiřazeno rezerv: **{len(assignments)}**"


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
            result = await assign_reserves(channel, manual=False)
            print(result)


@bot.event
async def on_ready():
    bot.add_view(AttendanceView())
    synced = await bot.tree.sync()
    print(f"Attendance bot online as {bot.user}")
    print(f"Synced {len(synced)} commands")

    if not reserve_checker.is_running():
        reserve_checker.start()


@bot.tree.command(name="team_add", description="Admin: přidat nebo upravit tým")
@app_commands.describe(team="Název týmu", driver1="První jezdec", driver2="Druhý jezdec")
async def team_add(interaction: discord.Interaction, team: str, driver1: discord.Member, driver2: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Nemáš oprávnění.", ephemeral=True)
        return

    cursor.execute("""
        INSERT INTO teams (team_name, driver1_id, driver1_name, driver2_id, driver2_name)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(team_name) DO UPDATE SET
            driver1_id = excluded.driver1_id,
            driver1_name = excluded.driver1_name,
            driver2_id = excluded.driver2_id,
            driver2_name = excluded.driver2_name
    """, (team, driver1.id, driver1.display_name, driver2.id, driver2.display_name))
    conn.commit()

    await interaction.response.send_message(
        f"✅ Tým **{team}** uložen:\n• {driver1.display_name}\n• {driver2.display_name}",
        ephemeral=True
    )


@bot.tree.command(name="team_remove", description="Admin: odstranit tým")
@app_commands.describe(team="Název týmu")
async def team_remove(interaction: discord.Interaction, team: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Nemáš oprávnění.", ephemeral=True)
        return

    cursor.execute("DELETE FROM teams WHERE LOWER(team_name) = LOWER(?)", (team,))
    conn.commit()
    await interaction.response.send_message(f"🗑️ Tým **{team}** byl odstraněn.", ephemeral=True)


@bot.tree.command(name="teams", description="Ukáže seznam týmů")
async def teams(interaction: discord.Interaction):
    cursor.execute("""
        SELECT team_name, driver1_id, driver2_id
        FROM teams
        ORDER BY team_name ASC
    """)
    rows = cursor.fetchall()

    embed = discord.Embed(title="🏎️ CSL TEAMS", color=discord.Color.blurple())
    if not rows:
        embed.description = "Zatím nejsou nastavené žádné týmy."
    else:
        for team_name, d1_id, d2_id in rows:
            embed.add_field(name=f"🏁 {team_name}", value=f"• <@{d1_id}>\n• <@{d2_id}>", inline=True)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="attendance_create", description="Admin: vytvořit attendance check")
@app_commands.describe(
    race="Název závodu, např. Race 16 - Surfers Paradise 🇦🇺",
    date="Datum, např. 23.5.2026",
    time_text="Čas závodu, např. 20:00",
    reserve_minutes_before="Kolik minut před závodem přiřadit rezervy, např. 60"
)
async def attendance_create(interaction: discord.Interaction, race: str, date: str, time_text: str, reserve_minutes_before: int = 60):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Nemáš oprávnění.", ephemeral=True)
        return

    try:
        race_ts = parse_race_datetime(date, time_text)
    except Exception:
        await interaction.response.send_message("❌ Špatný formát data nebo času. Použij např. `23.5.2026` a `20:00`.", ephemeral=True)
        return

    reserve_ts = race_ts - reserve_minutes_before * 60
    cursor.execute("DELETE FROM attendance_votes")
    cursor.execute("DELETE FROM reserve_assignments")
    conn.commit()

    set_event(race, race_ts, reserve_ts, interaction.channel.id, 0, 1, 0)
    await interaction.response.send_message(embed=create_attendance_embed(), view=AttendanceView())
    msg = await interaction.original_response()
    set_event(race, race_ts, reserve_ts, msg.channel.id, msg.id, 1, 0)


@bot.tree.command(name="attendance_status", description="Ukáže attendance status")
async def attendance_status(interaction: discord.Interaction):
    await interaction.response.send_message(embed=create_attendance_embed())


@bot.tree.command(name="attendance_close", description="Admin: uzavřít attendance check")
async def attendance_close(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Nemáš oprávnění.", ephemeral=True)
        return

    event = get_event()
    if not event:
        await interaction.response.send_message("❌ Žádný attendance check není aktivní.", ephemeral=True)
        return

    race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned = event
    set_event(race_name, race_ts, reserve_ts, channel_id, message_id, 0, reserves_assigned)
    await update_attendance_message()
    await interaction.response.send_message("🔒 Attendance check byl uzavřen.", ephemeral=True)


@bot.tree.command(name="reserve_assign", description="Admin: ručně přiřadit rezervy")
async def reserve_assign(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Nemáš oprávnění.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    result = await assign_reserves(interaction.channel, manual=True)
    await interaction.followup.send(result, ephemeral=True)


@bot.tree.command(name="attendance_reset", description="Admin: reset attendance odpovědí")
@app_commands.describe(confirm="Napiš RESET pro potvrzení")
async def attendance_reset(interaction: discord.Interaction, confirm: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Nemáš oprávnění.", ephemeral=True)
        return

    if confirm != "RESET":
        await interaction.response.send_message("❌ Pro potvrzení napiš přesně `RESET`.", ephemeral=True)
        return

    cursor.execute("DELETE FROM attendance_votes")
    cursor.execute("DELETE FROM reserve_assignments")
    conn.commit()

    event = get_event()
    if event:
        race_name, race_ts, reserve_ts, channel_id, message_id, is_open, reserves_assigned = event
        set_event(race_name, race_ts, reserve_ts, channel_id, message_id, is_open, 0)
        await update_attendance_message()

    await interaction.response.send_message("✅ Attendance odpovědi byly resetovány.", ephemeral=True)


if not TOKEN:
    raise RuntimeError("Missing ATTENDANCE_BOT_TOKEN in environment variables.")

bot.run(TOKEN)
