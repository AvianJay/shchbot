from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import shutil

import aiohttp
import discord

from school_discord_bot.config import Settings
from school_discord_bot.db.database import Database
from school_discord_bot.services.forum_poster import ForumPoster
from school_discord_bot.services.school_news_client import SchoolNewsClient
from school_discord_bot.services.tag_mapper import TagMapper


async def clear_forum_threads(forum: discord.ForumChannel) -> tuple[int, int]:
    deleted_active = 0
    deleted_archived = 0

    active_threads = [
        thread
        for thread in await forum.guild.active_threads()
        if thread.parent_id == forum.id
    ]
    seen_ids = {thread.id for thread in active_threads}
    for thread in active_threads:
        await thread.delete(reason="Reset forum before 100-post backfill")
        deleted_active += 1

    async for thread in forum.archived_threads(limit=200):
        if thread.id in seen_ids:
            continue
        await thread.delete(reason="Reset forum before 100-post backfill")
        deleted_archived += 1

    return deleted_active, deleted_archived


def backup_and_reset_database(database_path: Path) -> Path | None:
    if not database_path.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = database_path.with_name(f"{database_path.stem}.backup-{timestamp}{database_path.suffix}")
    shutil.copy2(database_path, backup_path)
    database_path.unlink()
    return backup_path


async def main() -> None:
    settings = Settings.from_env()
    project_root = Path(__file__).resolve().parent
    database_path = settings.resolve_database_path(project_root)
    backup_path = backup_and_reset_database(database_path)

    database = Database(database_path)
    await database.initialize()

    client = discord.Client(intents=discord.Intents.default())

    async with aiohttp.ClientSession() as session:
        school_news_client = SchoolNewsClient(
            session=session,
            widget_url=settings.school_news_widget_url,
            timeout_seconds=settings.http_timeout_seconds,
            user_agent=settings.user_agent,
            allow_insecure_ssl_fallback=settings.allow_insecure_school_ssl_fallback,
        )
        tag_mapper = TagMapper(database)
        forum_poster = ForumPoster(
            tag_mapper=tag_mapper,
            dry_run=False,
            allowed_mentions=settings.announcement_allowed_mentions,
            announcement_mention_prefix=settings.announcement_mention_prefix,
        )

        @client.event
        async def on_ready() -> None:
            try:
                forum = await forum_poster.validate_forum_channel(
                    bot=client,
                    channel_id=settings.announcement_forum_channel_id,
                )

                deleted_active, deleted_archived = await clear_forum_threads(forum)
                sync_result = await tag_mapper.sync_known_tags(forum)
                print("FETCH\tstart\tlimit=100\torder=oldest-first")
                announcements = await school_news_client.fetch_latest_announcements(
                    limit=100,
                    include_details=False,
                )

                detailed_announcements = []
                total = len(announcements)
                for index, announcement in enumerate(announcements, start=1):
                    print(f"FETCH\t{index}/{total}\t{announcement.date}\t{announcement.title}")
                    detailed_announcements.append(await school_news_client.enrich_announcement(announcement))

                announcements = list(reversed(detailed_announcements))
                print(f"FETCH\tdone\ttotal={len(announcements)}")

                posted_count = 0
                failed_count = 0
                total_posts = len(announcements)
                for index, announcement in enumerate(announcements, start=1):
                    await database.save_announcement(announcement)
                    stored = await database.get_announcement_by_hash(announcement.source_hash)
                    if stored is None:
                        stored = announcement
                    print(f"POSTING\t{index}/{total_posts}\t{stored.date}\t{stored.title}")
                    try:
                        result = await forum_poster.post_announcement(
                            forum=forum,
                            announcement=stored,
                            force_dry_run=False,
                        )
                    except Exception as exc:
                        failed_count += 1
                        print(f"FAILED\t{stored.source_id}\t{stored.title}\t{type(exc).__name__}: {exc}")
                        continue

                    if result.posted and result.thread_id is not None:
                        posted_count += 1
                        await database.mark_announcement_posted(stored.source_hash, result.thread_id)
                        print(
                            "POSTED\t"
                            f"{index}/{total_posts}\t"
                            f"{stored.title}\t"
                            f"tags={','.join(result.applied_tag_names) or '無'}\t"
                            f"thread_id={result.thread_id}"
                        )

                print(f"BACKUP\t{backup_path if backup_path else 'none'}")
                print(f"FORUM_CLEARED\tactive={deleted_active}\tarchived={deleted_archived}")
                print(
                    "TAGS\t"
                    f"created={','.join(sync_result.created) or 'none'}\t"
                    f"mapped={','.join(sync_result.mapped) or 'none'}\t"
                    f"missing_permissions={sync_result.missing_permissions}"
                )
                print(f"SUMMARY\tscanned={len(announcements)}\tposted={posted_count}\tfailed={failed_count}")
            finally:
                await client.close()

        try:
            await client.start(settings.discord_token)
        finally:
            await database.close()


if __name__ == "__main__":
    asyncio.run(main())
