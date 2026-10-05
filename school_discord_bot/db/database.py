from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
import sqlite3
from typing import Any

import aiosqlite

from school_discord_bot.db.migrations import apply_migrations
from school_discord_bot.models.announcement import Announcement
from school_discord_bot.models.anonymous_board import (
    AnonymousCategory,
    AnonymousPost,
    PostStatus,
)
from school_discord_bot.models.curriculum import ClassTimetable


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

    async def upsert_class_timetable(self, timetable: ClassTimetable) -> None:
        await self._execute(
            """
            INSERT INTO class_timetables (
                class_code, grade, schedule_title, homeroom_teacher, grid_json, fetched_at
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(class_code) DO UPDATE SET
                grade = excluded.grade,
                schedule_title = excluded.schedule_title,
                homeroom_teacher = excluded.homeroom_teacher,
                grid_json = excluded.grid_json,
                fetched_at = CURRENT_TIMESTAMP
            """,
            (
                timetable.class_code,
                timetable.grade,
                timetable.schedule_title,
                timetable.homeroom_teacher,
                timetable.to_grid_json(),
            ),
        )

    async def get_class_timetable(self, class_code: str) -> ClassTimetable | None:
        row = await self._fetchone(
            "SELECT * FROM class_timetables WHERE class_code = ?",
            (class_code,),
        )
        return ClassTimetable.from_database_row(row) if row else None

    async def list_class_codes(self, grade: str | None = None) -> list[str]:
        if grade:
            rows = await self._fetchall(
                "SELECT class_code FROM class_timetables WHERE grade = ? ORDER BY class_code ASC",
                (grade,),
            )
        else:
            rows = await self._fetchall(
                "SELECT class_code FROM class_timetables ORDER BY class_code ASC"
            )
        return [str(row["class_code"]) for row in rows]

    async def count_class_timetables(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS total FROM class_timetables")
        return int(row["total"] if row is not None else 0)

    async def get_user_class(self, user_id: str | int) -> str | None:
        """Return the saved class code for a Discord user, or None."""
        row = await self._fetchone(
            "SELECT class_code FROM user_preferences WHERE user_id = ?",
            (str(user_id),),
        )
        return str(row["class_code"]) if row else None

    async def set_user_class(self, user_id: str | int, class_code: str) -> None:
        """Save or update the preferred class code for a Discord user."""
        await self._execute(
            """
            INSERT INTO user_preferences (user_id, class_code, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                class_code = excluded.class_code,
                updated_at = CURRENT_TIMESTAMP
            """,
            (str(user_id), class_code),
        )

    async def is_student_verified(self, user_id: str | int) -> bool:
        """Return True if the Discord user has already completed student verification."""
        row = await self._fetchone(
            "SELECT 1 FROM verified_students WHERE user_id = ?",
            (str(user_id),),
        )
        return row is not None

    async def upsert_pending_verification(
        self,
        *,
        user_id: str | int,
        student_id: str,
        code: str,
        expires_at: float,
        last_sent_at: float,
    ) -> None:
        """Save or overwrite a pending verification record for a Discord user."""
        await self._execute(
            """
            INSERT INTO student_verifications (user_id, student_id, code, expires_at, last_sent_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                student_id   = excluded.student_id,
                code         = excluded.code,
                expires_at   = excluded.expires_at,
                last_sent_at = excluded.last_sent_at
            """,
            (str(user_id), student_id, code, expires_at, last_sent_at),
        )

    async def get_pending_verification(self, user_id: str | int) -> dict[str, Any] | None:
        """Return the pending verification record for a Discord user, or None."""
        row = await self._fetchone(
            "SELECT * FROM student_verifications WHERE user_id = ?",
            (str(user_id),),
        )
        return dict(row) if row else None

    async def delete_pending_verification(self, user_id: str | int) -> None:
        """Remove a pending verification record (used after success or expiry cleanup)."""
        await self._execute(
            "DELETE FROM student_verifications WHERE user_id = ?",
            (str(user_id),),
        )

    async def get_student_id_owner(self, student_id: str) -> str | None:
        """Return the Discord user ID already bound to this student ID, or None."""
        row = await self._fetchone(
            "SELECT user_id FROM verified_students WHERE student_id = ?",
            (student_id,),
        )
        return row["user_id"] if row else None

    async def insert_verified_student(
        self,
        *,
        user_id: str | int,
        student_id: str,
        verified_at: float,
    ) -> None:
        """Record a successfully verified student."""
        await self._execute(
            """
            INSERT INTO verified_students (user_id, student_id, verified_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                student_id  = excluded.student_id,
                verified_at = excluded.verified_at
            """,
            (str(user_id), student_id, verified_at),
        )

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

    async def list_anonymous_categories(self) -> list[AnonymousCategory]:
        rows = await self._fetchall(
            "SELECT id, name FROM anonymous_categories ORDER BY id ASC"
        )
        return [AnonymousCategory.from_database_row(row) for row in rows]

    async def count_anonymous_categories(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS total FROM anonymous_categories")
        return int(row["total"] if row is not None else 0)

    async def add_anonymous_category(self, name: str) -> bool:
        """Add a category; return False if one with this name already exists."""
        try:
            await self._execute(
                "INSERT INTO anonymous_categories (name) VALUES (?)",
                (name,),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    async def remove_anonymous_category(self, name: str) -> bool:
        removed = await self._execute(
            "DELETE FROM anonymous_categories WHERE name = ?",
            (name,),
        )
        return removed > 0

    async def seed_anonymous_categories(self, names: Iterable[str]) -> None:
        for name in names:
            await self._execute(
                "INSERT OR IGNORE INTO anonymous_categories (name) VALUES (?)",
                (name,),
            )

    async def create_anonymous_post(
        self,
        *,
        author_id: str | int,
        author_name: str,
        category_name: str,
        content: str,
        image_count: int,
        created_at: float,
        cooldown_seconds: float,
    ) -> int | None:
        """Insert a pending post unless the author posted within the cooldown.

        The cooldown check and the insert are a single statement, so two modals
        submitted at the same moment cannot both slip through. Returns the new
        post ID, or None while the author is still cooling down.
        """
        return await self._insert(
            """
            INSERT INTO anonymous_posts (
                author_id, author_name, category_name, content, image_count, status, created_at
            )
            SELECT ?, ?, ?, ?, ?, 'pending', ?
            WHERE NOT EXISTS (
                SELECT 1 FROM anonymous_posts
                WHERE author_id = ? AND created_at > ?
            )
            """,
            (
                str(author_id),
                author_name,
                category_name,
                content,
                image_count,
                created_at,
                str(author_id),
                created_at - cooldown_seconds,
            ),
        )

    async def get_anonymous_post(self, post_id: int) -> AnonymousPost | None:
        row = await self._fetchone(
            "SELECT * FROM anonymous_posts WHERE id = ?",
            (post_id,),
        )
        return AnonymousPost.from_database_row(row) if row else None

    async def get_last_anonymous_post_at(self, author_id: str | int) -> float | None:
        row = await self._fetchone(
            "SELECT MAX(created_at) AS last_at FROM anonymous_posts WHERE author_id = ?",
            (str(author_id),),
        )
        if row is None or row["last_at"] is None:
            return None
        return float(row["last_at"])

    async def set_anonymous_post_review_message(
        self,
        post_id: int,
        *,
        channel_id: int,
        message_id: int,
    ) -> None:
        await self._execute(
            """
            UPDATE anonymous_posts
            SET review_channel_id = ?,
                review_message_id = ?
            WHERE id = ?
            """,
            (channel_id, message_id, post_id),
        )

    async def delete_anonymous_post(self, post_id: int) -> None:
        await self._execute("DELETE FROM anonymous_posts WHERE id = ?", (post_id,))

    async def next_anonymous_public_number(self) -> int:
        row = await self._fetchone(
            "SELECT COALESCE(MAX(public_number), 0) + 1 AS next_number FROM anonymous_posts"
        )
        return int(row["next_number"]) if row is not None else 1

    async def mark_anonymous_post_published(
        self,
        post_id: int,
        *,
        public_number: int,
        channel_id: int,
        message_id: int,
        moderator_id: str | int | None,
        moderated_at: float,
    ) -> bool:
        """Record a pending post as published; return False if it was not pending."""
        changed = await self._execute(
            """
            UPDATE anonymous_posts
            SET status = 'published',
                public_number = ?,
                public_channel_id = ?,
                public_message_id = ?,
                moderator_id = ?,
                moderator_reason = NULL,
                moderated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (
                public_number,
                channel_id,
                message_id,
                str(moderator_id) if moderator_id is not None else None,
                moderated_at,
                post_id,
            ),
        )
        return changed == 1

    async def mark_anonymous_post_rejected(
        self,
        post_id: int,
        *,
        moderator_id: str | int,
        reason: str | None,
        moderated_at: float,
    ) -> bool:
        """Record a pending post as rejected; return False if it was not pending."""
        return await self._moderate_anonymous_post(
            post_id,
            from_status=PostStatus.PENDING,
            to_status=PostStatus.REJECTED,
            moderator_id=moderator_id,
            reason=reason,
            moderated_at=moderated_at,
        )

    async def mark_anonymous_post_removed(
        self,
        post_id: int,
        *,
        moderator_id: str | int,
        reason: str | None,
        moderated_at: float,
    ) -> bool:
        """Record a published post as taken down; return False if it was not published."""
        return await self._moderate_anonymous_post(
            post_id,
            from_status=PostStatus.PUBLISHED,
            to_status=PostStatus.REMOVED,
            moderator_id=moderator_id,
            reason=reason,
            moderated_at=moderated_at,
        )

    async def count_anonymous_posts(self, status: PostStatus) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS total FROM anonymous_posts WHERE status = ?",
            (str(status),),
        )
        return int(row["total"] if row is not None else 0)

    async def _moderate_anonymous_post(
        self,
        post_id: int,
        *,
        from_status: PostStatus,
        to_status: PostStatus,
        moderator_id: str | int,
        reason: str | None,
        moderated_at: float,
    ) -> bool:
        changed = await self._execute(
            """
            UPDATE anonymous_posts
            SET status = ?,
                moderator_id = ?,
                moderator_reason = ?,
                moderated_at = ?
            WHERE id = ? AND status = ?
            """,
            (str(to_status), str(moderator_id), reason, moderated_at, post_id, str(from_status)),
        )
        return changed == 1

    async def _execute(self, query: str, params: tuple[Any, ...] = ()) -> int:
        """Run a write statement and return the number of rows it changed."""
        connection = self._require_connection()
        async with self._write_lock:
            cursor = await connection.execute(query, params)
            await connection.commit()
            return cursor.rowcount

    async def _insert(self, query: str, params: tuple[Any, ...] = ()) -> int | None:
        """Run an INSERT and return the new row ID, or None if no row was inserted.

        ``lastrowid`` is left stale when a conditional ``INSERT … SELECT`` inserts
        nothing, so it is only trusted when exactly one row went in.
        """
        connection = self._require_connection()
        async with self._write_lock:
            cursor = await connection.execute(query, params)
            await connection.commit()
            return cursor.lastrowid if cursor.rowcount == 1 else None

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