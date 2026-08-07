from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
import json
from typing import Any


# Taiwan has observed no daylight saving time since 1979, so a fixed offset is
# exact. This avoids depending on an IANA database: ``ZoneInfo("Asia/Taipei")``
# raises ``ZoneInfoNotFoundError`` on a bare Windows host (no system tzdb, and
# ``tzdata`` is not a dependency), while the container runs UTC.
TAIPEI_TZ = timezone(timedelta(hours=8), "Asia/Taipei")

WEEKDAY_NAMES: tuple[str, ...] = ("星期一", "星期二", "星期三", "星期四", "星期五")
SCHOOL_DAYS = len(WEEKDAY_NAMES)

# The grid exposes nine slots per day, but only the periods below have a known
# bell time. Anything beyond period 8 renders without a time label.
PERIOD_START: dict[int, time] = {
    1: time(8, 10),
    2: time(9, 10),
    3: time(10, 15),
    4: time(11, 15),
    5: time(13, 10),
    6: time(14, 10),
    7: time(15, 10),
    8: time(16, 10),
}
PERIOD_MINUTES = 50
MAX_GRID_PERIOD = 9


def period_start_time(period: int) -> time | None:
    return PERIOD_START.get(period)


def period_end_time(period: int) -> time | None:
    start = PERIOD_START.get(period)
    if start is None:
        return None
    return (
        datetime.combine(datetime.min.date(), start) + timedelta(minutes=PERIOD_MINUTES)
    ).time()


def format_period_range(period: int) -> str:
    start = period_start_time(period)
    end = period_end_time(period)
    if start is None or end is None:
        return ""
    return f"{start:%H:%M}-{end:%H:%M}"


def now_in_taipei() -> datetime:
    return datetime.now(TAIPEI_TZ)


class PeriodState(StrEnum):
    WEEKEND = "weekend"
    BEFORE_SCHOOL = "before_school"
    IN_PERIOD = "in_period"
    BREAK = "break"
    AFTER_SCHOOL = "after_school"


@dataclass(slots=True)
class NowStatus:
    """Where the wall clock sits relative to the bell schedule."""

    state: PeriodState
    period: int | None = None
    next_period: int | None = None

    @property
    def is_school_time(self) -> bool:
        return self.state in {PeriodState.IN_PERIOD, PeriodState.BREAK}

    def describe(self) -> str:
        if self.state is PeriodState.WEEKEND:
            return "今天是假日，沒有課程"
        if self.state is PeriodState.BEFORE_SCHOOL:
            return f"還沒上課，第 {self.next_period} 節即將開始"
        if self.state is PeriodState.IN_PERIOD:
            return f"目前是第 {self.period} 節（{format_period_range(self.period)}）"
        if self.state is PeriodState.BREAK:
            if self.next_period is not None:
                return f"下課時間，下一節是第 {self.next_period} 節"
            return "下課時間"
        return "今天已經放學了"


def resolve_now(now: datetime, *, max_period: int) -> NowStatus:
    """Classify ``now`` against the bell schedule.

    ``max_period`` is the last period this timetable actually uses, so a
    six-period summer schedule reports ``AFTER_SCHOOL`` at 15:00 rather than
    pointing at an empty period 7.
    """
    if now.weekday() >= SCHOOL_DAYS:
        return NowStatus(state=PeriodState.WEEKEND)

    periods = sorted(p for p in PERIOD_START if p <= max_period)
    if not periods:
        return NowStatus(state=PeriodState.AFTER_SCHOOL)

    current = now.time()
    first = periods[0]
    if current < PERIOD_START[first]:
        return NowStatus(state=PeriodState.BEFORE_SCHOOL, next_period=first)

    for index, period in enumerate(periods):
        start = PERIOD_START[period]
        end = period_end_time(period)
        if end is None:
            continue
        if start <= current < end:
            following = periods[index + 1] if index + 1 < len(periods) else None
            return NowStatus(
                state=PeriodState.IN_PERIOD,
                period=period,
                next_period=following,
            )
        if current < start:
            # Past the previous period's end but not yet at this one's start.
            return NowStatus(
                state=PeriodState.BREAK,
                period=periods[index - 1] if index else None,
                next_period=period,
            )

    return NowStatus(state=PeriodState.AFTER_SCHOOL, period=periods[-1])


@dataclass(slots=True)
class Lesson:
    day: int  # 0 = Monday .. 4 = Friday
    period: int  # 1-based
    subject: str
    teachers: list[str] = field(default_factory=list)

    @property
    def teacher_text(self) -> str:
        return "、".join(self.teachers)


@dataclass(slots=True)
class ClassTimetable:
    class_code: str
    grade: str
    schedule_title: str = ""
    homeroom_teacher: str = ""
    lessons: list[Lesson] = field(default_factory=list)
    fetched_at: str | None = None

    def max_period(self) -> int:
        """Highest period that actually has a lesson.

        Derived rather than hardcoded: the schedule is six periods during the
        summer supplementary term and up to eight during a normal semester.
        """
        return max((lesson.period for lesson in self.lessons), default=0)

    def lessons_for_day(self, day: int) -> list[Lesson]:
        return sorted(
            (lesson for lesson in self.lessons if lesson.day == day),
            key=lambda lesson: lesson.period,
        )

    def lesson_at(self, day: int, period: int) -> Lesson | None:
        for lesson in self.lessons:
            if lesson.day == day and lesson.period == period:
                return lesson
        return None

    def to_grid_json(self) -> str:
        payload = {"lessons": [asdict(lesson) for lesson in self.lessons]}
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_database_row(cls, row: Any) -> "ClassTimetable":
        payload = json.loads(row["grid_json"]) if row["grid_json"] else {}
        lessons = [Lesson(**item) for item in payload.get("lessons", [])]
        return cls(
            class_code=str(row["class_code"]),
            grade=str(row["grade"] or ""),
            schedule_title=str(row["schedule_title"] or ""),
            homeroom_teacher=str(row["homeroom_teacher"] or ""),
            lessons=lessons,
            fetched_at=row["fetched_at"],
        )
