from __future__ import annotations

import json
import logging

import aiosqlite

from school_discord_bot.models.announcement import (
    build_source_hash,
    sanitize_url,
    unescape_js_slashes,
)


logger = logging.getLogger(__name__)


SCHEMA_STATEMENTS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
    """
    CREATE TABLE IF NOT EXISTS announcements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        source_hash TEXT NOT NULL UNIQUE,
        source_url TEXT,
        title TEXT NOT NULL,
        date TEXT NOT NULL,
        category TEXT NOT NULL,
        unit TEXT NOT NULL,
        excerpt TEXT,
        raw_json TEXT NOT NULL,
        view_count INTEGER,
        inner_tag_text TEXT,
        first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        posted_at TEXT,
        discord_thread_id INTEGER
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_announcements_date
    ON announcements (date DESC, id DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_announcements_posted_at
    ON announcements (posted_at DESC);
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS tag_mappings (
        category TEXT PRIMARY KEY,
        forum_tag_id INTEGER,
        forum_tag_name TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS category_subscriptions (
        category TEXT PRIMARY KEY,
        role_id INTEGER,
        enabled INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS keyword_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT NOT NULL UNIQUE,
        role_id INTEGER,
        enabled INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS class_timetables (
        class_code TEXT PRIMARY KEY,
        grade TEXT NOT NULL,
        schedule_title TEXT,
        homeroom_teacher TEXT,
        grid_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_class_timetables_grade
    ON class_timetables (grade, class_code);
    """,
    """
    CREATE TABLE IF NOT EXISTS user_preferences (
        user_id TEXT PRIMARY KEY,
        class_code TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
)


async def apply_migrations(connection: aiosqlite.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        await connection.execute(statement)
    await connection.commit()
    await repair_escaped_urls(connection)


def _repair_raw_json(raw_json: str) -> str:
    """Undo JavaScript slash escaping in the URL-bearing parts of ``raw_json``."""
    try:
        payload = json.loads(raw_json) if raw_json else {}
    except json.JSONDecodeError:
        return raw_json
    if not isinstance(payload, dict):
        return raw_json

    raw_payload = payload.get("raw_payload")
    if isinstance(raw_payload, dict) and isinstance(raw_payload.get("root_path"), str):
        raw_payload["root_path"] = unescape_js_slashes(raw_payload["root_path"])

    for key in ("attachments", "external_links"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                item["url"] = unescape_js_slashes(item["url"])

    return json.dumps(payload, ensure_ascii=False)


async def repair_escaped_urls(connection: aiosqlite.Connection) -> tuple[int, int]:
    """Repair announcements whose URLs were stored with escaped slashes.

    The school widget declares ``g_root_path`` as a JavaScript string literal, so
    an earlier version of the scraper persisted URLs like
    ``https:\\/\\/host\\/path``. Discord rejects those with
    ``embeds.0.url: Not a well formed URL``, which stalled the whole poll loop.

    Idempotent: rows without escaped slashes are left untouched. Returns
    ``(repaired, deleted)``.
    """
    connection.row_factory = aiosqlite.Row
    async with connection.execute(
        r"SELECT * FROM announcements WHERE source_url LIKE '%\/%'"
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows:
        return (0, 0)

    repaired = 0
    deleted = 0
    for row in rows:
        repaired_url = sanitize_url(row["source_url"])
        if repaired_url is None:
            continue

        repaired_hash = build_source_hash(
            repaired_url,
            date=row["date"] or "",
            category=row["category"] or "",
            unit=row["unit"] or "",
            title=row["title"] or "",
        )

        async with connection.execute(
            "SELECT id FROM announcements WHERE source_hash = ? AND id != ?",
            (repaired_hash, row["id"]),
        ) as cursor:
            twin = await cursor.fetchone()

        if twin is not None:
            # A correctly-scraped row for this announcement already exists, so the
            # escaped one is a duplicate that was never posted. Drop it.
            await connection.execute("DELETE FROM announcements WHERE id = ?", (row["id"],))
            deleted += 1
            continue

        await connection.execute(
            "UPDATE announcements SET source_url = ?, source_hash = ?, raw_json = ? WHERE id = ?",
            (repaired_url, repaired_hash, _repair_raw_json(row["raw_json"]), row["id"]),
        )
        repaired += 1

    await connection.commit()
    logger.info(
        "Repaired %s announcement rows with escaped URLs and removed %s duplicates",
        repaired,
        deleted,
    )
    return (repaired, deleted)
