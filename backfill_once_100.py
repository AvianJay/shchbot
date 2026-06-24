import asyncio
import logging
from pathlib import Path

import aiohttp
import discord

from school_discord_bot.config import Settings
from school_discord_bot.db.database import Database
from school_discord_bot.services.forum_poster import ForumPoster
from school_discord_bot.services.school_news_client import SchoolNewsClient
from school_discord_bot.services.tag_mapper import TagMapper


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    settings = Settings.from_env()
    project_root = Path(__file__).resolve().parent
    database = Database(settings.resolve_database_path(project_root))
    await database.initialize()

    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)

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

        @bot.event
        async def on_ready() -> None:
            try:
                forum = await forum_poster.validate_forum_channel(
                    bot=bot,
                    channel_id=settings.announcement_forum_channel_id,
                )
                announcements = await school_news_client.fetch_latest_announcements(
                    limit=100,
                    include_details=True,
                )
                posted_count = 0
                scanned = len(announcements)
                skipped_existing = 0
                failed_count = 0
                for announcement in announcements:
                    await database.save_announcement(announcement)
                    stored = await database.get_announcement_by_hash(announcement.source_hash)
                    if stored is None:
                        stored = announcement
                    if stored.posted_at:
                        skipped_existing += 1
                        continue
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
                        print(f"POSTED\t{stored.title}\t{result.thread_id}")
                print(f"SUMMARY\tscanned={scanned}\tposted={posted_count}\tskipped_existing={skipped_existing}\tfailed={failed_count}")
            finally:
                await database.close()
                await bot.close()

        await bot.start(settings.discord_token)


asyncio.run(main())
