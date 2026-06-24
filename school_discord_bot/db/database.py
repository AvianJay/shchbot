from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite

from school_discord_bot.db.migrations import apply_migrations
from school_discord_bot.models.announcement import Announcement


class Database:
    """SQLite persistence layer for scraped announcements and bot settings."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.database_path.as_posix())
        self._connection.row_factory = aiosqlite.Row
        await apply_migrations(self._connection)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def count_announcements(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS total FROM announcements")
        return int(row["total"] if row is not None else 0)

    async def get_announcement_by_hash(self, source_hash: str) -> Announcement | None:
        row = await self._fetchone(
            "SELECT * FROM announcements WHERE source_hash = ?",
            (source_hash,),
        )
        return Announcement.from_database_row(row) if row else None

    async def save_announcement(self, announcement: Announcement) -> bool:
        existing = await self.get_announcement_by_hash(announcement.source_hash)
        if existing is None:
            await self._execute(
                """
                INSERT INTO announcements (
                    source_id,
                    source_hash,
                    source_url,
                    title,
                    date,
                    category,
                    unit,
                    excerpt,
                    raw_json,
                    view_count,
                    inner_tag_text,
                    posted_at,
                    discord_thread_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._announcement_values(announcement),
            )
            return True

        merged = self._merge_announcements(existing, announcement)
        await self._execute(
            """
            UPDATE announcements
            SET source_id = ?,
                source_url = ?,
                title = ?,
                date = ?,
                category = ?,
                unit = ?,
                excerpt = ?,
                raw_json = ?,
                view_count = ?,
                inner_tag_text = ?,
                posted_at = ?,
                discord_thread_id = ?
            WHERE source_hash = ?
            """,
            (
                merged.source_id,
                merged.source_url,
                merged.title,
                merged.date,
                merged.category,
                merged.unit,
                merged.excerpt,
                merged.to_raw_json(),
                merged.view_count,
                merged.inner_tag_text,
                merged.posted_at,
                merged.discord_thread_id,
                merged.source_hash,
            ),
        )
        return False

    async def mark_announcement_posted(self, source_hash: str, discord_thread_id: int) -> None:
        await self._execute(
            """
            UPDATE announcements
            SET posted_at = CURRENT_TIMESTAMP,
                discord_thread_id = ?
            WHERE source_hash = ?
            """,
            (discord_thread_id, source_hash),
        )

    async def list_announcements(
        self,
        *,
        limit: int = 5,
        category: str | None = None,
        unit: str | None = None,
        keyword: str | None = None,
        only_posted: bool | None = None,
    ) -> list[Announcement]:
        query = ["SELECT * FROM announcements WHERE 1 = 1"]
        params: list[Any] = []

        if category:
            query.append("AND category = ?")
            params.append(category)
        if unit:
            query.append("AND unit = ?")
            params.append(unit)
        if keyword:
            query.append("AND (title LIKE ? OR excerpt LIKE ?)")
            like_value = f"%{keyword}%"
            params.extend([like_value, like_value])
        if only_posted is True:
            query.append("AND posted_at IS NOT NULL")
        elif only_posted is False:
            query.append("AND posted_at IS NULL")

        query.append("ORDER BY REPLACE(date, '/', '-') DESC, id DESC LIMIT ?")
        params.append(limit)

        rows = await self._fetchall(" ".join(query), tuple(params))
        return [Announcement.from_database_row(row) for row in rows]

    async def search_announcements(self, keyword: str, *, limit: int = 10) -> list[Announcement]:
        return await self.list_announcements(keyword=keyword, limit=limit)

    async def get_last_posted_announcement(self) -> Announcement | None:
        row = await self._fetchone(
            """
            SELECT * FROM announcements
            WHERE posted_at IS NOT NULL
            ORDER BY posted_at DESC, id DESC
            LIMIT 1
            """,
        )
        return Announcement.from_database_row(row) if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = await self._fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return default
        return str(row["value"])

    async def upsert_tag_mapping(
        self,
        *,
        category: str,
        forum_tag_id: int | None,
        forum_tag_name: str | None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO tag_mappings (category, forum_tag_id, forum_tag_name, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(category) DO UPDATE SET
                forum_tag_id = excluded.forum_tag_id,
                forum_tag_name = excluded.forum_tag_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            (category, forum_tag_id, forum_tag_name),
        )

    async def get_tag_mapping(self, category: str) -> dict[str, Any] | None:
        row = await self._fetchone(
            "SELECT * FROM tag_mappings WHERE category = ?",
            (category,),
        )
        return dict(row) if row else None

    async def list_tag_mappings(self) -> dict[str, dict[str, Any]]:
        rows = await self._fetchall("SELECT * FROM tag_mappings ORDER BY category ASC")
        return {str(row["category"]): dict(row) for row in rows}

    async def _execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        connection = self._require_connection()
        async with self._write_lock:
            await connection.execute(query, params)
            await connection.commit()

    async def _fetchone(
        self,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> aiosqlite.Row | None:
        connection = self._require_connection()
        async with connection.execute(query, params) as cursor:
            return await cursor.fetchone()

    async def _fetchall(
        self,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> list[aiosqlite.Row]:
        connection = self._require_connection()
        async with connection.execute(query, params) as cursor:
            return await cursor.fetchall()

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("database has not been initialized")
        return self._connection

    def _announcement_values(self, announcement: Announcement) -> tuple[Any, ...]:
        return (
            announcement.source_id,
            announcement.source_hash,
            announcement.source_url,
            announcement.title,
            announcement.date,
            announcement.category,
            announcement.unit,
            announcement.excerpt,
            announcement.to_raw_json(),
            announcement.view_count,
            announcement.inner_tag_text,
            announcement.posted_at,
            announcement.discord_thread_id,
        )

    def _merge_announcements(
        self,
        current: Announcement,
        incoming: Announcement,
    ) -> Announcement:
        return Announcement(
            source_id=incoming.source_id or current.source_id,
            source_hash=current.source_hash,
            source_url=incoming.source_url or current.source_url,
            title=incoming.title or current.title,
            date=incoming.date or current.date,
            category=incoming.category or current.category,
            unit=incoming.unit or current.unit,
            excerpt=incoming.excerpt or current.excerpt,
            raw_payload=incoming.raw_payload or current.raw_payload,
            view_count=incoming.view_count if incoming.view_count is not None else current.view_count,
            inner_tag_text=incoming.inner_tag_text or current.inner_tag_text,
            content_html=incoming.content_html or current.content_html,
            content_text=incoming.content_text or current.content_text,
            attachments=incoming.attachments or current.attachments,
            external_links=incoming.external_links or current.external_links,
            important_dates=incoming.important_dates or current.important_dates,
            posted_at=current.posted_at,
            discord_thread_id=current.discord_thread_id,
        )