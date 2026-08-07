from __future__ import annotations

import asyncio
from pathlib import Path

from school_discord_bot.db.database import Database
from school_discord_bot.models.curriculum import ClassTimetable, Lesson


def _timetable(class_code: str, grade: str, lessons: list[Lesson] | None = None) -> ClassTimetable:
    return ClassTimetable(
        class_code=class_code,
        grade=grade,
        schedule_title="115學年暑假輔導課表",
        homeroom_teacher="導師：測試",
        lessons=lessons
        if lessons is not None
        else [
            Lesson(day=0, period=1, subject="導師時間", teachers=["蔡怡安"]),
            Lesson(day=4, period=5, subject="化學", teachers=["蔡怡安"]),
        ],
    )


async def _fresh_db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "curriculum.sqlite3")
    await database.initialize()
    return database


def test_upsert_and_get_round_trip(tmp_path: Path) -> None:
    async def run() -> None:
        database = await _fresh_db(tmp_path)
        try:
            await database.upsert_class_timetable(_timetable("205", "2年級"))
            stored = await database.get_class_timetable("205")

            assert stored is not None
            assert stored.class_code == "205"
            assert stored.grade == "2年級"
            assert stored.homeroom_teacher == "導師：測試"
            assert len(stored.lessons) == 2
            assert stored.lessons_for_day(0)[0].subject == "導師時間"
            # fetched_at is filled in by the database default.
            assert stored.fetched_at
        finally:
            await database.close()

    asyncio.run(run())


def test_get_returns_none_for_unknown_class(tmp_path: Path) -> None:
    async def run() -> None:
        database = await _fresh_db(tmp_path)
        try:
            assert await database.get_class_timetable("999") is None
        finally:
            await database.close()

    asyncio.run(run())


def test_upsert_replaces_existing_row(tmp_path: Path) -> None:
    """Re-fetching a class must overwrite, not duplicate or append."""

    async def run() -> None:
        database = await _fresh_db(tmp_path)
        try:
            await database.upsert_class_timetable(_timetable("205", "2年級"))
            await database.upsert_class_timetable(
                _timetable(
                    "205",
                    "2年級",
                    lessons=[Lesson(day=1, period=3, subject="新課程", teachers=["新老師"])],
                )
            )

            assert await database.count_class_timetables() == 1
            stored = await database.get_class_timetable("205")
            assert stored is not None
            assert len(stored.lessons) == 1
            assert stored.lessons[0].subject == "新課程"
        finally:
            await database.close()

    asyncio.run(run())


def test_list_class_codes_filters_by_grade_and_sorts(tmp_path: Path) -> None:
    async def run() -> None:
        database = await _fresh_db(tmp_path)
        try:
            for code, grade in (
                ("205", "2年級"),
                ("201", "2年級"),
                ("101", "1年級"),
                ("301", "3年級"),
            ):
                await database.upsert_class_timetable(_timetable(code, grade))

            assert await database.list_class_codes("2年級") == ["201", "205"]
            assert await database.list_class_codes("1年級") == ["101"]
            assert await database.list_class_codes("3年級") == ["301"]
            assert await database.list_class_codes("4年級") == []
        finally:
            await database.close()

    asyncio.run(run())


def test_count_starts_at_zero(tmp_path: Path) -> None:
    async def run() -> None:
        database = await _fresh_db(tmp_path)
        try:
            assert await database.count_class_timetables() == 0
        finally:
            await database.close()

    asyncio.run(run())


def test_empty_lessons_survive_round_trip(tmp_path: Path) -> None:
    """A class with a title but no lessons must not come back as None."""

    async def run() -> None:
        database = await _fresh_db(tmp_path)
        try:
            await database.upsert_class_timetable(_timetable("218", "2年級", lessons=[]))
            stored = await database.get_class_timetable("218")

            assert stored is not None
            assert stored.lessons == []
            assert stored.max_period() == 0
        finally:
            await database.close()

    asyncio.run(run())


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    """Re-initializing must not drop existing curriculum rows."""

    async def run() -> None:
        path = tmp_path / "curriculum.sqlite3"
        first = Database(path)
        await first.initialize()
        await first.upsert_class_timetable(_timetable("205", "2年級"))
        await first.close()

        second = Database(path)
        await second.initialize()
        try:
            assert await second.count_class_timetables() == 1
            assert await second.get_class_timetable("205") is not None
        finally:
            await second.close()

    asyncio.run(run())
