from __future__ import annotations

from datetime import datetime

import pytest

from school_discord_bot.models.curriculum import (
    PERIOD_MINUTES,
    PERIOD_START,
    TAIPEI_TZ,
    ClassTimetable,
    Lesson,
    PeriodState,
    resolve_now,
)


def at(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """Build a Taipei-local datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=TAIPEI_TZ)


# 2026-08-07 is a Friday; 2026-08-08 a Saturday; 2026-08-10 a Monday.
FRIDAY = (2026, 8, 7)
SATURDAY = (2026, 8, 8)
SUNDAY = (2026, 8, 9)
MONDAY = (2026, 8, 10)


@pytest.mark.parametrize("date", [SATURDAY, SUNDAY])
def test_weekend_reports_weekend(date: tuple[int, int, int]) -> None:
    status = resolve_now(at(*date, 10, 30), max_period=6)
    assert status.state is PeriodState.WEEKEND
    assert status.period is None
    assert status.next_period is None


def test_before_first_period_reports_before_school() -> None:
    status = resolve_now(at(*FRIDAY, 7, 0), max_period=6)
    assert status.state is PeriodState.BEFORE_SCHOOL
    assert status.period is None
    assert status.next_period == 1


def test_exactly_at_first_period_start_is_in_period() -> None:
    status = resolve_now(at(*FRIDAY, 8, 10), max_period=6)
    assert status.state is PeriodState.IN_PERIOD
    assert status.period == 1


def test_mid_first_period_is_in_period() -> None:
    status = resolve_now(at(*FRIDAY, 8, 30), max_period=6)
    assert status.state is PeriodState.IN_PERIOD
    assert status.period == 1
    assert status.next_period == 2


def test_between_periods_reports_break() -> None:
    # Period 1 runs 08:10-09:00; period 2 starts 09:10. 09:05 is a break.
    status = resolve_now(at(*FRIDAY, 9, 5), max_period=6)
    assert status.state is PeriodState.BREAK
    assert status.period == 1
    assert status.next_period == 2


def test_lunch_break_is_break_not_after_school() -> None:
    # Period 4 ends 12:05, period 5 starts 13:10.
    status = resolve_now(at(*FRIDAY, 12, 30), max_period=6)
    assert status.state is PeriodState.BREAK
    assert status.period == 4
    assert status.next_period == 5


def test_last_period_end_is_after_school() -> None:
    # With 6 periods, period 6 ends at 15:00.
    status = resolve_now(at(*FRIDAY, 15, 0), max_period=6)
    assert status.state is PeriodState.AFTER_SCHOOL


def test_after_school_on_six_period_schedule() -> None:
    status = resolve_now(at(*FRIDAY, 15, 30), max_period=6)
    assert status.state is PeriodState.AFTER_SCHOOL
    assert status.next_period is None


def test_same_time_is_still_class_on_eight_period_schedule() -> None:
    """The 6-vs-8 period boundary: 15:30 is period 7 on a full schedule."""
    status = resolve_now(at(*FRIDAY, 15, 30), max_period=8)
    assert status.state is PeriodState.IN_PERIOD
    assert status.period == 7


def test_late_evening_is_after_school_even_on_full_schedule() -> None:
    status = resolve_now(at(*FRIDAY, 20, 0), max_period=8)
    assert status.state is PeriodState.AFTER_SCHOOL


def test_monday_early_morning_is_before_school() -> None:
    status = resolve_now(at(*MONDAY, 6, 0), max_period=6)
    assert status.state is PeriodState.BEFORE_SCHOOL
    assert status.next_period == 1


def test_max_period_is_clamped_to_known_period_times() -> None:
    """A 9-period grid cannot run past the last known start time."""
    status = resolve_now(at(*FRIDAY, 23, 0), max_period=9)
    assert status.state is PeriodState.AFTER_SCHOOL


def test_every_period_start_resolves_to_that_period() -> None:
    for period, start in PERIOD_START.items():
        status = resolve_now(at(*FRIDAY, start.hour, start.minute), max_period=8)
        assert status.state is PeriodState.IN_PERIOD
        assert status.period == period


def test_period_boundaries_do_not_overlap() -> None:
    """The minute a period ends must not still report that period."""
    for period, start in PERIOD_START.items():
        end = start.hour * 60 + start.minute + PERIOD_MINUTES
        status = resolve_now(at(*FRIDAY, end // 60, end % 60), max_period=8)
        if status.state is PeriodState.IN_PERIOD:
            assert status.period != period


def _timetable(lessons: list[Lesson]) -> ClassTimetable:
    return ClassTimetable(
        class_code="205",
        grade="2年級",
        schedule_title="test",
        homeroom_teacher="導師：測試",
        lessons=lessons,
        fetched_at="2026-08-07 03:00:00",
    )


def test_max_period_reflects_populated_lessons() -> None:
    table = _timetable(
        [
            Lesson(day=0, period=1, subject="國語文", teachers=["A"]),
            Lesson(day=2, period=6, subject="自習", teachers=[]),
        ]
    )
    assert table.max_period() == 6


def test_max_period_on_empty_timetable_does_not_crash() -> None:
    assert _timetable([]).max_period() == 0


def test_lessons_for_day_is_sorted_by_period() -> None:
    table = _timetable(
        [
            Lesson(day=1, period=5, subject="E", teachers=[]),
            Lesson(day=1, period=2, subject="B", teachers=[]),
            Lesson(day=0, period=1, subject="other day", teachers=[]),
            Lesson(day=1, period=3, subject="C", teachers=[]),
        ]
    )
    lessons = table.lessons_for_day(1)
    assert [lesson.period for lesson in lessons] == [2, 3, 5]
    assert [lesson.subject for lesson in lessons] == ["B", "C", "E"]


def test_lessons_for_day_returns_empty_for_day_with_no_lessons() -> None:
    table = _timetable([Lesson(day=0, period=1, subject="X", teachers=[])])
    assert table.lessons_for_day(4) == []


def test_grid_json_round_trip_preserves_lessons() -> None:
    original = _timetable(
        [
            Lesson(day=0, period=1, subject="導師時間", teachers=["蔡怡安"]),
            Lesson(day=2, period=6, subject="自習", teachers=[]),
            Lesson(day=4, period=2, subject="高二自然加深", teachers=["林莉娟", "第二位"]),
        ]
    )
    payload = original.to_grid_json()

    row = {
        "class_code": original.class_code,
        "grade": original.grade,
        "schedule_title": original.schedule_title,
        "homeroom_teacher": original.homeroom_teacher,
        "grid_json": payload,
        "fetched_at": original.fetched_at,
    }
    restored = ClassTimetable.from_database_row(row)

    assert restored.class_code == original.class_code
    assert restored.max_period() == original.max_period()
    assert len(restored.lessons) == 3
    assert restored.lessons_for_day(4)[0].teachers == ["林莉娟", "第二位"]
    assert restored.lessons_for_day(2)[0].teachers == []
    assert restored.lessons_for_day(0)[0].subject == "導師時間"
