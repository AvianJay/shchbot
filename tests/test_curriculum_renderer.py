from __future__ import annotations

import pytest

from school_discord_bot.cogs.curriculum import _format_fetched_at
from school_discord_bot.models.curriculum import (
    ClassTimetable,
    Lesson,
    NowStatus,
    PeriodState,
)
from school_discord_bot.services.curriculum_renderer import (
    _resolve_font_path,
    render_week_image,
)


requires_font = pytest.mark.skipif(
    _resolve_font_path() is None,
    reason="no CJK font available on this machine",
)


def _timetable(lessons: list[Lesson] | None = None) -> ClassTimetable:
    return ClassTimetable(
        class_code="205",
        grade="2年級",
        schedule_title="115學年暑假輔導課表",
        homeroom_teacher="導師：蔡怡安",
        lessons=lessons
        if lessons is not None
        else [
            Lesson(day=day, period=period, subject="國語文", teachers=["林玉珊"])
            for day in range(5)
            for period in range(1, 7)
        ],
    )


@requires_font
def test_render_returns_png() -> None:
    buffer = render_week_image(
        _timetable(),
        now=NowStatus(state=PeriodState.IN_PERIOD, period=1, next_period=2),
        today=0,
    )
    data = buffer.getvalue()

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(data) > 1000


@requires_font
def test_render_handles_weekend_without_today_column() -> None:
    buffer = render_week_image(
        _timetable(),
        now=NowStatus(state=PeriodState.WEEKEND),
        today=None,
    )
    assert buffer.getvalue().startswith(b"\x89PNG\r\n\x1a\n")


@requires_font
def test_render_handles_sparse_timetable() -> None:
    """Some classes have only a handful of lessons; that must still render."""
    buffer = render_week_image(
        _timetable([Lesson(day=2, period=3, subject="自習", teachers=[])]),
        now=NowStatus(state=PeriodState.AFTER_SCHOOL),
        today=2,
    )
    assert buffer.getvalue().startswith(b"\x89PNG\r\n\x1a\n")


@requires_font
def test_render_handles_empty_timetable() -> None:
    buffer = render_week_image(
        _timetable([]),
        now=NowStatus(state=PeriodState.WEEKEND),
        today=None,
    )
    assert buffer.getvalue().startswith(b"\x89PNG\r\n\x1a\n")


@requires_font
def test_render_grows_with_period_count() -> None:
    """An eight-period grid must be taller than a six-period one."""
    from PIL import Image

    six = Image.open(
        render_week_image(
            _timetable(
                [Lesson(day=0, period=p, subject="X", teachers=[]) for p in range(1, 7)]
            ),
            now=NowStatus(state=PeriodState.AFTER_SCHOOL),
            today=None,
        )
    )
    eight = Image.open(
        render_week_image(
            _timetable(
                [Lesson(day=0, period=p, subject="X", teachers=[]) for p in range(1, 9)]
            ),
            now=NowStatus(state=PeriodState.AFTER_SCHOOL),
            today=None,
        )
    )

    assert eight.height > six.height
    assert eight.width == six.width


def test_format_fetched_at_converts_utc_to_taipei() -> None:
    """SQLite stores CURRENT_TIMESTAMP in UTC with no timezone marker."""
    assert _format_fetched_at("2026-08-07 03:51:48") == "2026-08-07 11:51"


def test_format_fetched_at_handles_missing_and_invalid_values() -> None:
    assert _format_fetched_at(None) == ""
    assert _format_fetched_at("") == ""
    assert _format_fetched_at("not-a-timestamp") == "not-a-timestamp"
