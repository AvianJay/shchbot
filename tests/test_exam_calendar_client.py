from __future__ import annotations

import asyncio
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import aiohttp
import pytest

from school_discord_bot.db.database import Database
from school_discord_bot.models.exam import ExamType, parse_exam_date
from school_discord_bot.services.exam_calendar_client import (
    CACHE_SETTING_KEY,
    EXAM_CALENDAR_URL,
    ExamCalendarClient,
    ExamCalendarError,
    parse_exam_calendar,
    select_exam_date,
)


def event(code="gsat", start="2027-01-22", end="2027-01-24", kind="exam"):
    return {"type": kind, "exam": code, "start_date": start, "end_date": end}


def payload():
    return {"schema_version": 1, "events": [
        event(kind="registration", start="2026-10-27", end="2026-11-10"),
        event(kind="result", start="2027-02-25", end="2027-02-25"),
        event(),
        event("ast", "2027-07-10", "2027-07-11"),
        event("tcte", "2027-04-25", "2027-04-26"),
    ]}


def http_session(data=None, error=None):
    response = SimpleNamespace(
        raise_for_status=Mock(),
        json=AsyncMock(return_value=data if data is not None else payload()),
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response, side_effect=error)
    context.__aexit__ = AsyncMock(return_value=False)
    return SimpleNamespace(get=Mock(return_value=context)), response


def client(session, database):
    return ExamCalendarClient(session, database=database, timeout_seconds=20, user_agent="test-agent")


@pytest.mark.parametrize("value", ["2027-02-30", "20270122", "2027-W03-5", "2027/01/22", " 2027-01-22", "", None])
def test_invalid_dates_are_rejected(value) -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_exam_date(value)


def test_valid_leap_day() -> None:
    assert parse_exam_date("2028-02-29") == date(2028, 2, 29)


def test_only_exam_events_are_selected() -> None:
    schedules = parse_exam_calendar(payload())
    assert [(item.exam_type, item.start_date, item.end_date) for item in schedules] == [
        (ExamType.GSAT, date(2027, 1, 22), date(2027, 1, 24)),
        (ExamType.SUBJECT, date(2027, 7, 10), date(2027, 7, 11)),
    ]


@pytest.mark.parametrize("bad_event", [
    event(start="2027-02-30"),
    event(start=None),
    event(end="2027-01-21"),
])
def test_invalid_exam_event_does_not_discard_other_exam(bad_event) -> None:
    schedules = parse_exam_calendar({"events": [bad_event, event("ast", "2027-07-10", "2027-07-11")]})
    assert select_exam_date(schedules, ExamType.SUBJECT, date(2026, 10, 8)) == date(2027, 7, 10)
    with pytest.raises(ExamCalendarError, match="學測"):
        select_exam_date(schedules, ExamType.GSAT, date(2026, 10, 8))


@pytest.mark.parametrize("data", [None, [], {}, {"events": {}}, {"events": []}, {"events": [event(kind="registration")]}])
def test_invalid_calendar_is_rejected(data) -> None:
    with pytest.raises(ExamCalendarError):
        parse_exam_calendar(data)


def test_missing_end_date_is_a_single_day_exam() -> None:
    schedules = parse_exam_calendar({"events": [event(end=None)]})
    assert schedules[0].end_date == date(2027, 1, 22)


@pytest.mark.parametrize(("today", "expected"), [
    (date(2026, 10, 8), date(2027, 1, 22)),
    (date(2027, 1, 22), date(2027, 1, 22)),
    (date(2027, 1, 23), date(2027, 1, 22)),
    (date(2027, 1, 24), date(2027, 1, 22)),
    (date(2027, 1, 25), date(2028, 1, 21)),
    (date(2029, 1, 1), date(2028, 1, 21)),
])
def test_selects_nearest_ongoing_or_upcoming_exam_across_years(today, expected) -> None:
    schedules = parse_exam_calendar({"events": [
        event(start="2028-01-21", end="2028-01-23"),
        event(),
        event(start="2026-01-17", end="2026-01-19"),
    ]})
    assert select_exam_date(schedules, ExamType.GSAT, today) == expected


def test_no_next_year_keeps_last_exam_instead_of_inventing_date() -> None:
    schedules = parse_exam_calendar(payload())
    assert select_exam_date(schedules, ExamType.GSAT, date(2027, 1, 25)) == date(2027, 1, 22)


def test_fetches_raw_json_and_persists_validated_exam_events() -> None:
    async def run() -> None:
        session, response = http_session()
        database = SimpleNamespace(set_setting=AsyncMock(), get_setting=AsyncMock(return_value=None))
        calendar = client(session, database)
        assert await calendar.get_exam_date(ExamType.GSAT, today=date(2026, 10, 8)) == date(2027, 1, 22)
        assert await calendar.get_exam_date(ExamType.SUBJECT, today=date(2026, 10, 8)) == date(2027, 7, 10)
        session.get.assert_called_once()
        assert session.get.call_args.args == (EXAM_CALENDAR_URL,)
        assert session.get.call_args.kwargs["timeout"].total == 20
        assert session.get.call_args.kwargs["headers"] == {"User-Agent": "test-agent"}
        response.raise_for_status.assert_called_once()
        response.json.assert_awaited_once_with(content_type=None)
        assert database.set_setting.await_args.args[0] == CACHE_SETTING_KEY
        cached = json.loads(database.set_setting.await_args.args[1])
        assert len(cached["events"]) == 2
        assert all(item["type"] == "exam" for item in cached["events"])
        database.get_setting.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["connection", "timeout", "http", "format", "json"])
def test_restart_uses_persisted_cache_if_source_fails(tmp_path: Path, failure: str) -> None:
    async def run() -> None:
        database = Database(tmp_path / "calendar.sqlite3")
        await database.initialize()
        try:
            session, _ = http_session()
            await client(session, database).refresh()
            await database.close()
            await database.initialize()
            broken_session, response = http_session(
                error=aiohttp.ClientConnectionError() if failure == "connection" else TimeoutError() if failure == "timeout" else None
            )
            if failure == "http":
                response.raise_for_status.side_effect = aiohttp.ClientResponseError(Mock(), (), status=503)
            elif failure == "format":
                response.json.return_value = {"events": "wrong schema"}
            elif failure == "json":
                response.json.side_effect = ValueError("invalid JSON")
            restarted = client(broken_session, database)
            await restarted.refresh()
            assert await restarted.get_exam_date(ExamType.GSAT, today=date(2026, 10, 8)) == date(2027, 1, 22)
            assert await restarted.get_exam_date(ExamType.SUBJECT, today=date(2026, 10, 8)) == date(2027, 7, 10)
            assert len(parse_exam_calendar(json.loads(await database.get_setting(CACHE_SETTING_KEY)))) == 2
        finally:
            await database.close()

    asyncio.run(run())


@pytest.mark.parametrize("cached", [None, "invalid JSON", '{"events": []}'])
def test_first_fetch_failure_with_no_valid_cache_is_reported(cached) -> None:
    async def run() -> None:
        session, _ = http_session(error=aiohttp.ClientConnectionError())
        database = SimpleNamespace(get_setting=AsyncMock(return_value=cached), set_setting=AsyncMock())
        with pytest.raises(ExamCalendarError, match="沒有可用快取"):
            await client(session, database).get_exam_date(ExamType.GSAT, today=date(2026, 10, 8))
        database.set_setting.assert_not_awaited()

    asyncio.run(run())


def test_memory_cache_survives_outage_and_later_receives_corrected_date() -> None:
    async def run() -> None:
        session, response = http_session()
        database = SimpleNamespace(set_setting=AsyncMock(), get_setting=AsyncMock(return_value=None))
        calendar = client(session, database)
        await calendar.refresh()
        response.json.side_effect = ValueError("invalid JSON")
        await calendar.refresh()
        assert await calendar.get_exam_date(ExamType.GSAT, today=date(2026, 10, 8)) == date(2027, 1, 22)
        response.json.side_effect = None
        response.json.return_value = {"events": [event(start="2027-01-23", end="2027-01-25")]}
        await calendar.refresh()
        assert await calendar.get_exam_date(ExamType.GSAT, today=date(2026, 10, 8)) == date(2027, 1, 23)

    asyncio.run(run())
