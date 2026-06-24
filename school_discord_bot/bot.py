from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

from school_discord_bot.cogs.admin import AdminCog
from school_discord_bot.cogs.announcements import AnnouncementsCog
from school_discord_bot.config import Settings
from school_discord_bot.db.database import Database
from school_discord_bot.services.forum_poster import ForumPoster
from school_discord_bot.services.school_news_client import SchoolNewsClient
from school_discord_bot.services.tag_mapper import TagMapper


class SchoolDiscordBot(commands.Bot):
    def __init__(self, settings: Settings, database: Database) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.database = database
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        school_news_client = SchoolNewsClient(
            session=self.http_session,
            widget_url=self.settings.school_news_widget_url,
            timeout_seconds=self.settings.http_timeout_seconds,
            user_agent=self.settings.user_agent,
            allow_insecure_ssl_fallback=self.settings.allow_insecure_school_ssl_fallback,
        )
        tag_mapper = TagMapper(self.database)
        forum_poster = ForumPoster(tag_mapper=tag_mapper, dry_run=self.settings.dry_run)

        admin_cog = AdminCog(
            self,
            database=self.database,
            school_news_client=school_news_client,
            forum_poster=forum_poster,
            tag_mapper=tag_mapper,
            guild_id=self.settings.guild_id,
            forum_channel_id=self.settings.announcement_forum_channel_id,
            dry_run=self.settings.dry_run,
        )
        announcements_cog = AnnouncementsCog(
            self,
            database=self.database,
            school_news_client=school_news_client,
            forum_poster=forum_poster,
            forum_channel_id=self.settings.announcement_forum_channel_id,
            poll_interval_seconds=self.settings.poll_interval_seconds,
            dry_run=self.settings.dry_run,
        )

        await self.add_cog(admin_cog)
        await self.add_cog(announcements_cog)
        self.tree.copy_global_to(guild=discord.Object(id=self.settings.guild_id))
        await self.tree.sync(guild=discord.Object(id=self.settings.guild_id))

    async def close(self) -> None:
        if self.http_session is not None and not self.http_session.closed:
            await self.http_session.close()
        await super().close()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def async_main() -> None:
    configure_logging()
    project_root = Path(__file__).resolve().parent.parent
    settings = Settings.from_env()
    database = Database(settings.resolve_database_path(project_root))
    await database.initialize()

    bot = SchoolDiscordBot(settings, database)
    try:
        await bot.start(settings.discord_token)
    finally:
        await database.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()