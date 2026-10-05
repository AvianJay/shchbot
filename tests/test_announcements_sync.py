from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from school_discord_bot.cogs.announcements import AnnouncementsCog
from school_discord_bot.db.database import Database
from school_discord_bot.models.announcement import Announcement
from school_discord_bot.services.forum_poster import PostResult


def listed(source_id: str, date: str, *, pinned: bool = False) -> Announcement:
    return Announcement(
        source_id=source_id,
        source_hash=f"hash-{source_id}",
        source_url=f"https://example.com/news/{source_id}",
        title=f"公告 {source_id}",
        date=date,
        category="大學升學",
        unit="註冊組",
        pinned=pinned,
    )


def fake_post(*, forum: object, announcement: Announcement, force_dry_run: bool) -> PostResult:
    return PostResult(
        thread_id=None if force_dry_run else int(announcement.source_id),
        thread_title=announcement.title,
        applied_tag_names=[],
        posted=not force_dry_run,
    )


def build_cog(database: object, listing: list[Announcement]) -> tuple[AnnouncementsCog, SimpleNamespace, SimpleNamespace]:
    news_client = SimpleNamespace(
        fetch_latest_announcements=AsyncMock(return_value=listing),
        enrich_announcement=AsyncMock(side_effect=lambda announcement: replace(announcement, excerpt="公告內文")),
    )
    forum_poster = SimpleNamespace(
        validate_forum_channel=AsyncMock(return_value=object()),
        post_announcement=AsyncMock(side_effect=fake_post),
    )
    cog = AnnouncementsCog(
        SimpleNamespace(wait_until_ready=AsyncMock()),
        database=database,
        school_news_client=news_client,
        forum_poster=forum_poster,
        forum_channel_id=789,
        poll_interval_seconds=600,
        dry_run=False,
    )
    return cog, news_client, forum_poster


def test_background_poll_scans_the_whole_window_without_details() -> None:
    """Regression: the poll read only 5 rows, all of them pinned, so new posts never showed up."""

    async def run() -> None:
        cog, news_client, _ = build_cog(SimpleNamespace(), [])

        await cog.poll_announcements()

        news_client.fetch_latest_announcements.assert_awaited_once_with(
            limit=AnnouncementsCog.POLL_WINDOW,
            include_details=False,
        )
        # The busiest day on the school site so far (2026/08/28) had 22 announcements.
        assert AnnouncementsCog.POLL_WINDOW >= 22

    asyncio.run(run())


def test_sync_enriches_and_posts_only_unposted_announcements_oldest_first(tmp_path: Path) -> None:
    async def run() -> None:
        database = Database(tmp_path / "bot.sqlite3")
        await database.initialize()
        try:
            already_posted = listed("20375", "2026/09/30", pinned=True)
            await database.save_announcement(already_posted)
            await database.mark_announcement_posted(already_posted.source_hash, 1)
            failed_earlier = listed("20387", "2026/10/01")
            await database.save_announcement(failed_earlier)

            cog, news_client, forum_poster = build_cog(
                database,
                [already_posted, listed("20410", "2026/10/05"), failed_earlier, listed("20384", "2026/10/01")],
            )

            outcome = await cog.sync_announcements(limit=30, include_only_unposted=True, dry_run=False)

            enriched = [call.args[0].source_id for call in news_client.enrich_announcement.await_args_list]
            posted = [call.kwargs["announcement"].source_id for call in forum_poster.post_announcement.await_args_list]
            assert enriched == ["20384", "20387", "20410"]
            assert posted == ["20384", "20387", "20410"]
            assert (outcome.scanned, outcome.new_items, outcome.posted_items) == (4, 2, 3)

            stored = await database.get_announcement_by_hash("hash-20410")
            assert stored is not None
            assert stored.posted_at is not None
            assert stored.discord_thread_id == 20410
            assert stored.excerpt == "公告內文"
        finally:
            await database.close()

    asyncio.run(run())


def test_dry_run_preview_lists_newest_first_without_posting(tmp_path: Path) -> None:
    async def run() -> None:
        database = Database(tmp_path / "bot.sqlite3")
        await database.initialize()
        try:
            cog, _, forum_poster = build_cog(
                database,
                [listed("19951", "2026/06/30", pinned=True), listed("20410", "2026/10/05"), listed("20384", "2026/10/01")],
            )
            interaction = SimpleNamespace(
                response=SimpleNamespace(defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
            )

            await AnnouncementsCog.news_dry_run.callback(cog, interaction, 5)

            lines = interaction.followup.send.await_args.args[0].splitlines()
            assert [line.split(" | ")[0] for line in lines] == ["公告 20410", "公告 20384", "公告 19951"]
            assert all(call.kwargs["force_dry_run"] for call in forum_poster.post_announcement.await_args_list)
            assert (await database.get_announcement_by_hash("hash-20410")).posted_at is None
        finally:
            await database.close()

    asyncio.run(run())
