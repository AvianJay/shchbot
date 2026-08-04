from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import aiosqlite

from school_discord_bot.db.migrations import apply_migrations, repair_escaped_urls
from school_discord_bot.models.announcement import build_source_hash


ESCAPED_BASE = "https:\\/\\/www.dali.tc.edu.tw\\/ischool\\/"
CLEAN_BASE = "https://www.dali.tc.edu.tw/ischool/"
ESCAPED_NEWS_URL = f"{ESCAPED_BASE}public/news_view/show.php?nid={{nid}}"
CLEAN_NEWS_URL = f"{CLEAN_BASE}public/news_view/show.php?nid={{nid}}"


def legacy_hash(escaped_url: str) -> str:
    """The hash the pre-fix scraper stored: sha256 over the un-repaired URL."""
    return hashlib.sha256(escaped_url.encode("utf-8")).hexdigest()


def _raw_json(*, root_path: str, attachment_url: str) -> str:
    return json.dumps(
        {
            "source_id": "20065",
            "raw_payload": {"root_path": root_path},
            "view_count": 1,
            "inner_tag_text": None,
            "content_html": "<p>內容</p>",
            "content_text": "內容",
            "attachments": [{"name": "報名簡章.pdf", "url": attachment_url}],
            "external_links": [],
            "important_dates": [],
        },
        ensure_ascii=False,
    )


async def _insert(
    connection: aiosqlite.Connection,
    *,
    source_id: str,
    source_url: str,
    posted_at: str | None,
    thread_id: int | None,
    raw_json: str,
    source_hash: str | None = None,
) -> None:
    await connection.execute(
        """
        INSERT INTO announcements (
            source_id, source_hash, source_url, title, date, category, unit,
            excerpt, raw_json, view_count, inner_tag_text, posted_at, discord_thread_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            source_hash
            or build_source_hash(
                source_url, date="2026/08/04", category="競賽資訊", unit="設備組", title="標題"
            ),
            source_url,
            "標題",
            "2026/08/04",
            "競賽資訊",
            "設備組",
            "摘要",
            raw_json,
            1,
            None,
            posted_at,
            thread_id,
        ),
    )


async def _build_database(database_path: Path) -> aiosqlite.Connection:
    connection = await aiosqlite.connect(database_path.as_posix())
    connection.row_factory = aiosqlite.Row
    await apply_migrations(connection)
    return connection


def test_repair_escaped_urls_fixes_row_in_place_and_drops_duplicates(tmp_path: Path) -> None:
    async def scenario() -> None:
        connection = await _build_database(tmp_path / "bot.sqlite3")
        try:
            # An announcement scraped correctly and already posted...
            await _insert(
                connection,
                source_id="20048",
                source_url=CLEAN_NEWS_URL.format(nid="20048"),
                posted_at="2026-07-30 10:20:31",
                thread_id=1532332051200737492,
                raw_json=_raw_json(root_path=CLEAN_BASE, attachment_url=f"{CLEAN_BASE}a.pdf"),
            )
            # ...and the escaped duplicate the buggy scraper left behind.
            await _insert(
                connection,
                source_id="20048",
                source_url=ESCAPED_NEWS_URL.format(nid="20048"),
                source_hash=legacy_hash(ESCAPED_NEWS_URL.format(nid="20048")),
                posted_at=None,
                thread_id=None,
                raw_json=_raw_json(root_path=ESCAPED_BASE, attachment_url=f"{ESCAPED_BASE}a.pdf"),
            )
            # An escaped row with no clean twin: never posted, must be repaired.
            await _insert(
                connection,
                source_id="20065",
                source_url=ESCAPED_NEWS_URL.format(nid="20065"),
                source_hash=legacy_hash(ESCAPED_NEWS_URL.format(nid="20065")),
                posted_at=None,
                thread_id=None,
                raw_json=_raw_json(root_path=ESCAPED_BASE, attachment_url=f"{ESCAPED_BASE}b.pdf"),
            )
            await connection.commit()

            repaired, deleted = await repair_escaped_urls(connection)
            assert (repaired, deleted) == (1, 1)

            async with connection.execute(
                "SELECT * FROM announcements WHERE source_id = '20048'"
            ) as cursor:
                survivors = await cursor.fetchall()
            assert len(survivors) == 1
            assert survivors[0]["posted_at"] == "2026-07-30 10:20:31"
            assert survivors[0]["discord_thread_id"] == 1532332051200737492

            async with connection.execute(
                "SELECT * FROM announcements WHERE source_id = '20065'"
            ) as cursor:
                row = await cursor.fetchone()

            expected_url = CLEAN_NEWS_URL.format(nid="20065")
            assert row["source_url"] == expected_url
            assert row["source_hash"] == build_source_hash(
                expected_url, date="2026/08/04", category="競賽資訊", unit="設備組", title="標題"
            )
            # Still unposted, so the normal poll loop will publish it.
            assert row["posted_at"] is None

            payload = json.loads(row["raw_json"])
            assert payload["raw_payload"]["root_path"] == CLEAN_BASE
            assert payload["attachments"][0]["url"] == f"{CLEAN_BASE}b.pdf"
        finally:
            await connection.close()

    asyncio.run(scenario())


def test_repair_escaped_urls_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        connection = await _build_database(tmp_path / "bot.sqlite3")
        try:
            await _insert(
                connection,
                source_id="20065",
                source_url=ESCAPED_NEWS_URL.format(nid="20065"),
                source_hash=legacy_hash(ESCAPED_NEWS_URL.format(nid="20065")),
                posted_at=None,
                thread_id=None,
                raw_json=_raw_json(root_path=ESCAPED_BASE, attachment_url=f"{ESCAPED_BASE}b.pdf"),
            )
            await connection.commit()

            assert await repair_escaped_urls(connection) == (1, 0)
            assert await repair_escaped_urls(connection) == (0, 0)

            async with connection.execute("SELECT COUNT(*) AS total FROM announcements") as cursor:
                row = await cursor.fetchone()
            assert row["total"] == 1
        finally:
            await connection.close()

    asyncio.run(scenario())


def test_repair_escaped_urls_leaves_clean_rows_untouched(tmp_path: Path) -> None:
    async def scenario() -> None:
        connection = await _build_database(tmp_path / "bot.sqlite3")
        try:
            await _insert(
                connection,
                source_id="20048",
                source_url=CLEAN_NEWS_URL.format(nid="20048"),
                posted_at="2026-07-30 10:20:31",
                thread_id=1532332051200737492,
                raw_json=_raw_json(root_path=CLEAN_BASE, attachment_url=f"{CLEAN_BASE}a.pdf"),
            )
            await connection.commit()

            async with connection.execute("SELECT * FROM announcements") as cursor:
                before = dict(await cursor.fetchone())

            assert await repair_escaped_urls(connection) == (0, 0)

            async with connection.execute("SELECT * FROM announcements") as cursor:
                after = dict(await cursor.fetchone())
            assert before == after
        finally:
            await connection.close()

    asyncio.run(scenario())
