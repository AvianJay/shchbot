"""Tests for the curriculum scraper's HTML parsing.

Fixtures are real pages captured from the school site, so these cover the
quirks that matter: the subject living in a bare text node, cells with no
teacher, and the two distinct "no timetable" responses.
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from school_discord_bot.services.curriculum_client import (
    _class_codes_in,
    _extract_state,
    _grade_for_code,
    parse_timetable_page,
)


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def grade_html() -> str:
    return (FIXTURES / "sample_curriculum_grade.html").read_text(encoding="utf-8")


@pytest.fixture
def class_205_html() -> str:
    return (FIXTURES / "sample_curriculum_205.html").read_text(encoding="utf-8")


@pytest.fixture
def no_data_html() -> str:
    return (FIXTURES / "sample_curriculum_nodata.html").read_text(encoding="utf-8")


def test_parse_timetable_reads_metadata(class_205_html: str) -> None:
    timetable = parse_timetable_page(class_205_html, class_code="205", grade="2年級")

    assert timetable is not None
    assert timetable.class_code == "205"
    assert timetable.grade == "2年級"
    assert timetable.homeroom_teacher == "導師：蔡怡安"
    assert timetable.schedule_title == "115學年暑假輔導課表"


def test_parse_timetable_reads_full_grid(class_205_html: str) -> None:
    timetable = parse_timetable_page(class_205_html, class_code="205", grade="2年級")
    assert timetable is not None

    # Five weekdays x six periods in the summer supplementary schedule.
    assert len(timetable.lessons) == 30
    assert timetable.max_period() == 6

    for lesson in timetable.lessons:
        assert 0 <= lesson.day <= 4
        assert 1 <= lesson.period <= 9
        assert lesson.subject.strip()


def test_parse_timetable_extracts_subject_and_teacher(class_205_html: str) -> None:
    """The subject is a bare text node; the teacher is an anchor."""
    timetable = parse_timetable_page(class_205_html, class_code="205", grade="2年級")
    assert timetable is not None

    monday_first = timetable.lesson_at(0, 1)
    assert monday_first is not None
    assert monday_first.subject == "導師時間"
    assert monday_first.teachers == ["蔡怡安"]


def test_parse_timetable_keeps_lesson_without_teacher(class_205_html: str) -> None:
    """A 自習 period has a subject but no teacher anchor text."""
    timetable = parse_timetable_page(class_205_html, class_code="205", grade="2年級")
    assert timetable is not None

    wednesday_sixth = timetable.lesson_at(2, 6)
    assert wednesday_sixth is not None
    assert wednesday_sixth.subject == "自習"
    assert wednesday_sixth.teachers == []


def test_parse_timetable_returns_none_when_no_data(no_data_html: str) -> None:
    """A class listed in the dropdown but not running returns 查無資料."""
    assert parse_timetable_page(no_data_html, class_code="218", grade="2年級") is None


def test_parse_timetable_returns_none_for_garbage() -> None:
    assert parse_timetable_page("<html><body>nope</body></html>", class_code="205", grade="2年級") is None


def test_class_codes_in_lists_grade_classes(grade_html: str) -> None:
    codes = _class_codes_in(BeautifulSoup(grade_html, "lxml"))

    assert "205" in codes
    assert codes == sorted(codes)
    # Must stay within Discord's 25-option select cap.
    assert len(codes) <= 25


def test_extract_state_pulls_viewstate(grade_html: str) -> None:
    state = _extract_state(BeautifulSoup(grade_html, "lxml"))

    assert state["__VIEWSTATE"]
    assert state["__EVENTVALIDATION"]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("101", "1年級"),
        ("205", "2年級"),
        ("320", "3年級"),
        ("405", None),
        ("20", None),
        ("abc", None),
        ("", None),
    ],
)
def test_grade_for_code(code: str, expected: str | None) -> None:
    assert _grade_for_code(code) == expected
