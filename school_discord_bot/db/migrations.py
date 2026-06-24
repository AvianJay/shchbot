from __future__ import annotations

import aiosqlite


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
)


async def apply_migrations(connection: aiosqlite.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        await connection.execute(statement)
    await connection.commit()