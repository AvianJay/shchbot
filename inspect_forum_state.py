from __future__ import annotations

import asyncio
from pathlib import Path

import discord

from school_discord_bot.config import Settings
from school_discord_bot.db.database import Database
from school_discord_bot.services.forum_poster import ForumPoster
from school_discord_bot.services.tag_mapper import TagMapper


async def main() -> None:
    project_root = Path(__file__).resolve().parent
    settings = Settings.from_env()
    database = Database(settings.resolve_database_path(project_root))
    await database.initialize()

    client = discord.Client(intents=discord.Intents.default())
    forum_poster = ForumPoster(
        tag_mapper=TagMapper(database),
        dry_run=True,
        allowed_mentions=settings.announcement_allowed_mentions,
        announcement_mention_prefix=settings.announcement_mention_prefix,
    )
    try:
        await client.login(settings.discord_token)
        forum = await forum_poster.validate_forum_channel(
            bot=client,
            channel_id=settings.announcement_forum_channel_id,
        )
        active_threads = forum.threads
        archived_threads = [thread async for thread in forum.archived_threads(limit=200)]
        print(f"forum_name={forum.name}")
        print(f"available_tags={len(forum.available_tags)}")
        for tag in forum.available_tags:
            print(f"tag:{tag.name}:{tag.id}")
        print(f"active_threads={len(active_threads)}")
        print(f"archived_threads={len(archived_threads)}")
    finally:
        await client.close()
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
