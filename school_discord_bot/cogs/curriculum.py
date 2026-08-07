from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from school_discord_bot.db.database import Database
from school_discord_bot.models.curriculum import (
    PERIOD_START,
    TAIPEI_TZ,
    WEEKDAY_NAMES,
    ClassTimetable,
    NowStatus,
    PeriodState,
    format_period_range,
    now_in_taipei,
    resolve_now,
)
from school_discord_bot.services.curriculum_client import (
    ALL_GRADES,
    CurriculumClient,
    _grade_for_code,
)
from school_discord_bot.services.curriculum_renderer import (
    ensure_font_downloaded,
    render_week_image,
)

logger = logging.getLogger(__name__)

# Colour constants for embeds.
_COLOR_TIMETABLE = 0x4CAF50  # green — on-brand for a school schedule
_COLOR_WEEKEND = 0x78909C    # blue-grey — calm "no lessons" signal
_COLOR_NODATA = 0xFFA726     # orange — no timetable available for this class
_COLOR_PANEL = 0x2196F3      # blue — informational panel

# custom_id scheme for persistent grade buttons.
CUSTOM_GRADE_1 = "curriculum:grade:1"
CUSTOM_GRADE_2 = "curriculum:grade:2"
CUSTOM_GRADE_3 = "curriculum:grade:3"

_GRADE_CUSTOM_IDS: dict[str, str] = {
    "1年級": CUSTOM_GRADE_1,
    "2年級": CUSTOM_GRADE_2,
    "3年級": CUSTOM_GRADE_3,
}

# Reverse lookup: custom_id → grade.
_ID_TO_GRADE: dict[str, str] = {v: k for k, v in _GRADE_CUSTOM_IDS.items()}


# ---------------------------------------------------------------------------
# Shared timetable → embed + image helper
# ---------------------------------------------------------------------------


def _format_fetched_at(raw: str | None) -> str:
    """Render a stored fetch timestamp in Taipei local time.

    ``fetched_at`` is written by SQLite's ``CURRENT_TIMESTAMP``, which is UTC
    and carries no timezone marker, so it has to be tagged as UTC before
    converting. Unparseable values are passed through unchanged.
    """
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return str(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M")


async def build_timetable_response(
    timetable: ClassTimetable,
    *,
    now: NowStatus,
    today: int | None,
) -> tuple[discord.Embed, discord.File | None]:
    """Produce the embed and optional image that every lookup pathway uses."""

    now_text = now.describe()

    embed = discord.Embed(
        title=f"📘 {timetable.class_code} 課表",
        description=timetable.schedule_title or "課表",
        color=_COLOR_TIMETABLE,
        timestamp=datetime.now(TAIPEI_TZ),
    )
    embed.add_field(name="班級課表", value=now_text, inline=False)
    if timetable.homeroom_teacher:
        embed.add_field(name="導師", value=timetable.homeroom_teacher, inline=True)

    # Today's lessons as text.
    if today is not None and now.state is not PeriodState.WEEKEND:
        day_name = WEEKDAY_NAMES[today]
        lessons = timetable.lessons_for_day(today)
        if lessons:
            lines: list[str] = []
            for lesson in lessons:
                marker = ""
                if now.state is PeriodState.IN_PERIOD and now.period == lesson.period:
                    marker = " ▶"
                teachers = " — " + "、".join(lesson.teachers) if lesson.teachers else ""
                lines.append(f"{marker} **{lesson.period}** {lesson.subject}{teachers}")
            embed.add_field(
                name=f"{day_name} 今日課表",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(name=day_name, value="今天沒有課程", inline=False)
    else:
        embed.add_field(name="今日", value="今天是假日，沒有課程", inline=False)

    # Timestamp footer. SQLite's CURRENT_TIMESTAMP is UTC, so convert before
    # showing it or the time reads eight hours behind local.
    footer = _format_fetched_at(timetable.fetched_at)
    if footer:
        embed.set_footer(text=f"最後更新於 {footer}")

    # Full-week image.
    file = None
    try:
        buf = render_week_image(timetable, now=now, today=today)
        file = discord.File(buf, filename="timetable.png")
        embed.set_image(url="attachment://timetable.png")
    except Exception:
        logger.exception("Failed to render timetable image for %s", timetable.class_code)
        embed.add_field(
            name="⚠️",
            value="圖片生成失敗，但文字課表如上",
            inline=False,
        )

    return embed, file


# ---------------------------------------------------------------------------
# Ephemeral class-picker view (not persistent — timeout-bound)
# ---------------------------------------------------------------------------


class ClassSelect(discord.ui.Select):
    """Dropdown of class codes for one grade.

    Discord caps a select at 25 options; each grade here has at most 20
    classes, so one select is always enough. If a grade ever exceeds the cap
    the list is truncated and a warning logged rather than failing the
    interaction outright.
    """

    MAX_OPTIONS = 25

    def __init__(self, grade: str, class_codes: list[str]) -> None:
        if len(class_codes) > self.MAX_OPTIONS:
            logger.warning(
                "Grade %s has %s classes, exceeding Discord's %s-option select cap; truncating",
                grade,
                len(class_codes),
                self.MAX_OPTIONS,
            )
            class_codes = class_codes[: self.MAX_OPTIONS]

        super().__init__(
            placeholder=f"選擇 {grade} 的班級",
            options=[discord.SelectOption(label=code, value=code) for code in class_codes],
            min_values=1,
            max_values=1,
        )
        self.grade = grade

    async def callback(self, interaction: discord.Interaction) -> None:
        code = self.values[0] if self.values else None
        if not code:
            await interaction.response.send_message("❌ 未選擇班級", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        cog = interaction.client.cogs.get("CurriculumCog")
        if not isinstance(cog, CurriculumCog):
            await interaction.followup.send("❌ 課表模組尚未載入，請稍後再試", ephemeral=True)
            return

        content, embed, file = await cog.lookup_timetable(code)
        if file:
            await interaction.followup.send(content=content, embed=embed, file=file, ephemeral=True)
        else:
            await interaction.followup.send(content=content, embed=embed, ephemeral=True)


class CurriculumClassSelectView(discord.ui.View):
    """Ephemeral wrapper around :class:`ClassSelect`. Not persistent by design."""

    def __init__(self, grade: str, class_codes: list[str]) -> None:
        super().__init__(timeout=180)
        self.grade = grade
        self.add_item(ClassSelect(grade, class_codes))


# ---------------------------------------------------------------------------
# Persistent grade-panel view (survives bot restarts)
# ---------------------------------------------------------------------------


class GradeButton(discord.ui.Button):
    """One grade button. Holds its grade so the callback stays stateless.

    The callback must live on the item: ``discord.ui.View`` dispatches clicks
    to ``item.callback``, and the base ``Button.callback`` is a no-op, so a
    button added without one silently does nothing.
    """

    def __init__(self, grade: str, custom_id: str) -> None:
        super().__init__(
            label=grade,
            custom_id=custom_id,
            style=discord.ButtonStyle.primary,
        )
        self.grade = grade

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.cogs.get("CurriculumCog")
        if not isinstance(cog, CurriculumCog):
            await interaction.response.send_message("❌ 課表模組尚未載入", ephemeral=True)
            return

        codes = await cog.database.list_class_codes(self.grade)
        if not codes:
            await interaction.response.send_message(
                f"❌ {self.grade} 的課表資料尚未取得，請稍後再試",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"📂 選擇 {self.grade} 的班級：",
            view=CurriculumClassSelectView(self.grade, codes),
            ephemeral=True,
        )


class CurriculumPanelView(discord.ui.View):
    """Three persistent buttons: 一年級 / 二年級 / 三年級.

    Registered via ``bot.add_view`` in ``setup_hook`` so clicks work across
    restarts. This is the first persistent view in the repo — the existing
    ``SchoolLinksView`` only works because URL buttons never dispatch
    interactions.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for grade, custom_id in _GRADE_CUSTOM_IDS.items():
            self.add_item(GradeButton(grade, custom_id))

# ---------------------------------------------------------------------------
# Autocomplete helper (must be defined before the class that references it)
# ---------------------------------------------------------------------------


async def _autocomplete_class_code(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    cog = interaction.client.cogs.get("CurriculumCog")
    if not isinstance(cog, CurriculumCog):
        return []
    codes = await cog.database.list_class_codes()
    if not codes:
        return []
    matches = [code for code in codes if current in code]
    return [
        app_commands.Choice(name=code, value=code)
        for code in matches[:25]
    ]


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class CurriculumCog(commands.Cog, name="CurriculumCog"):
    def __init__(
        self,
        bot: commands.Bot,
        *,
        database: Database,
        curriculum_client: CurriculumClient,
        guild_id: int,
        refresh_hours: int = 12,
    ) -> None:
        self.bot = bot
        self.database = database
        self.curriculum_client = curriculum_client
        self.guild_id = guild_id
        self.refresh_hours = refresh_hours
        self.logger = logging.getLogger(__name__)
        self.prefetch_timetables.change_interval(hours=self.refresh_hours)

    async def cog_load(self) -> None:
        if not self.prefetch_timetables.is_running():
            self.prefetch_timetables.start()

    async def cog_unload(self) -> None:
        self.prefetch_timetables.cancel()

    # ------------------------------------------------------------------
    # Background prefetch
    # ------------------------------------------------------------------

    @tasks.loop(hours=12)
    async def prefetch_timetables(self) -> None:
        try:
            self.logger.info("Starting timetable prefetch…")
            timetables = await self.curriculum_client.fetch_all()
            for tt in timetables:
                await self.database.upsert_class_timetable(tt)
            await self.database.set_setting(
                "curriculum_last_full_sync",
                datetime.now(TAIPEI_TZ).isoformat(),
            )
            self.logger.info("Prefetch complete: %s timetable(s) stored", len(timetables))
        except Exception:
            self.logger.exception("Timetable prefetch failed")

    @prefetch_timetables.before_loop
    async def before_prefetch(self) -> None:
        await self.bot.wait_until_ready()
        # Fetch the CJK font on first run if none is available locally. It is
        # not committed to the repo (see ensure_font_downloaded), and this runs
        # before the first render rather than blocking cog_load.
        await ensure_font_downloaded(self.curriculum_client.session)

    # ------------------------------------------------------------------
    # /課表 <class_code>
    # ------------------------------------------------------------------

    @app_commands.command(name="課表", description="查詢班級今日課表")
    @app_commands.describe(班級="班級代號，例如 205")
    @app_commands.autocomplete(班級=_autocomplete_class_code)
    async def curriculum_lookup(
        self,
        interaction: discord.Interaction,
        班級: app_commands.Range[str, 3, 4],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        content, embed, file = await self.lookup_timetable(班級)
        if file:
            await interaction.followup.send(content=content, embed=embed, file=file, ephemeral=True)
        else:
            await interaction.followup.send(content=content, embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /school send_curriculum  → the admin that posts the panel
    # ------------------------------------------------------------------

    # (Defined inline here for import by AdminCog — see bot.py wiring.)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def lookup_timetable(self, class_code: str) -> tuple[str, discord.Embed, discord.File | None]:
        """Shared lookup path used by the slash command and the class select."""
        weekday = now_in_taipei().weekday()
        today = None if weekday >= 5 else weekday

        # Try DB cache first.
        tt = await self.database.get_class_timetable(class_code)
        if tt is None:
            # Cache miss — fall back to a live fetch.
            grade = _grade_for_code(class_code)
            if grade is None:
                return (
                    "❌ 班級代號格式錯誤，請輸入如 205 的格式",
                    discord.Embed(title="格式錯誤", color=_COLOR_NODATA),
                    None,
                )

            try:
                tt = await self.curriculum_client.fetch_class_timetable(class_code)
            except Exception:
                self.logger.exception("Live fetch failed for class %s", class_code)
                return (
                    "❌ 無法取得課表，學校網站可能暫時無法連線",
                    discord.Embed(title="連線錯誤", description="請稍後再試", color=_COLOR_NODATA),
                    None,
                )

            if tt is None:
                return (
                    f"❌ 找不到班級 **{class_code}** 的課表，該班級可能不在目前的開課範圍內",
                    discord.Embed(
                        title="查無課表",
                        description=f"班級 {class_code} 目前沒有課表資料",
                        color=_COLOR_NODATA,
                    ),
                    None,
                )

            # Store it so subsequent queries hit the cache.
            await self.database.upsert_class_timetable(tt)

        # Now recompute now-status against the actual timetable.
        now = resolve_now(now_in_taipei(), max_period=tt.max_period())

        embed, file = await build_timetable_response(tt, now=now, today=today)
        return "", embed, file

    async def post_panel(self, channel: discord.TextChannel) -> None:
        """Post the persistent grade-button panel to a channel."""
        embed = discord.Embed(
            title="📚 班級課表查詢",
            description=(
                "點擊下方按鈕選擇**年級**，接著選擇**班級**即可查看該班的今日課表。\n\n"
                "也可以直接輸入指令 `/課表 205` 快速查詢。"
            ),
            color=_COLOR_PANEL,
        )
        view = CurriculumPanelView()
        await channel.send(embed=embed, view=view)

# vim: syntax=python



# The admin panel-posting command lives on AdminCog (/school send_curriculum),
# since that cog owns the /school command group. It calls post_panel above.
