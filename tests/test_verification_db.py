from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from school_discord_bot.db.database import Database


async def _fresh_db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "bot.sqlite3")
    await database.initialize()
    return database


def _legacy_verified_students(path: Path, rows: list[tuple[str, str, float]]) -> None:
    """Create a pre-uniqueness verified_students table holding ``rows``."""
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE verified_students (
            user_id     TEXT PRIMARY KEY,
            student_id  TEXT NOT NULL,
            verified_at REAL NOT NULL
        )
        """
    )
    connection.executemany("INSERT INTO verified_students VALUES (?, ?, ?)", rows)
    connection.commit()
    connection.close()


def test_student_id_can_only_bind_one_account(tmp_path: Path) -> None:
    async def run() -> None:
        database = await _fresh_db(tmp_path)
        try:
            await database.insert_verified_student(
                user_id=111, student_id="310200", verified_at=1.0
            )

            with pytest.raises(sqlite3.IntegrityError):
                await database.insert_verified_student(
                    user_id=222, student_id="310200", verified_at=2.0
                )

            rows = await database._fetchall("SELECT user_id FROM verified_students")
            assert [row["user_id"] for row in rows] == ["111"]
        finally:
            await database.close()

    asyncio.run(run())


def test_same_account_can_reverify_same_student_id(tmp_path: Path) -> None:
    async def run() -> None:
        database = await _fresh_db(tmp_path)
        try:
            await database.insert_verified_student(
                user_id=111, student_id="310200", verified_at=1.0
            )
            await database.insert_verified_student(
                user_id=111, student_id="310200", verified_at=5.0
            )

            rows = await database._fetchall("SELECT verified_at FROM verified_students")
            assert len(rows) == 1
            assert rows[0]["verified_at"] == 5.0
        finally:
            await database.close()

    asyncio.run(run())


def test_get_student_id_owner(tmp_path: Path) -> None:
    async def run() -> None:
        database = await _fresh_db(tmp_path)
        try:
            await database.insert_verified_student(
                user_id=111, student_id="310200", verified_at=1.0
            )

            assert await database.get_student_id_owner("310200") == "111"
            assert await database.get_student_id_owner("999999") is None
        finally:
            await database.close()

    asyncio.run(run())


def test_migration_adds_uniqueness_to_legacy_database(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "bot.sqlite3"
        _legacy_verified_students(path, [("1", "310200", 1.0), ("2", "310201", 2.0)])

        database = Database(path)
        await database.initialize()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                await database.insert_verified_student(
                    user_id=3, student_id="310200", verified_at=3.0
                )
        finally:
            await database.close()

    asyncio.run(run())


def test_migration_reports_preexisting_duplicate_bindings(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "bot.sqlite3"
        _legacy_verified_students(path, [("1", "310200", 1.0), ("2", "310200", 2.0)])

        database = Database(path)
        try:
            with pytest.raises(RuntimeError, match="310200"):
                await database.initialize()
        finally:
            await database.close()

    asyncio.run(run())


def test_pending_verification_round_trip(tmp_path: Path) -> None:
    async def run() -> None:
        database = await _fresh_db(tmp_path)
        try:
            await database.upsert_pending_verification(
                user_id=111,
                student_id="310200",
                code="000042",
                expires_at=9_000_000_000.0,
                last_sent_at=1.0,
            )

            pending = await database.get_pending_verification(111)
            assert pending is not None
            assert pending["student_id"] == "310200"
            # Leading zeros must survive the round trip.
            assert pending["code"] == "000042"
            assert pending["last_sent_at"] == 1.0

            await database.delete_pending_verification(111)
            assert await database.get_pending_verification(111) is None
        finally:
            await database.close()

    asyncio.run(run())
