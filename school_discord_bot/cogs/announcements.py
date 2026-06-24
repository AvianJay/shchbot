from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from school_discord_bot.db.database import Database
from school_discord_bot.models.announcement import Announcement, normalize_text
from school_discord_bot.services.forum_poster import ForumPoster, PostResult
from school_discord_bot.services.school_news_client import SchoolNewsClient


def admin_only() -> app_commands.Check:
    async def predicate(interaction: discord.Interaction) -> bool:
        permissions = interaction.user.guild_permissions
        return permissions.manage_guild or permissions.manage_channels

    return app_commands.check(predicate)


@dataclass(slots=True)
class SyncOutcome:
    scanned: int
    new_items: int
    posted_items: int
    results: list[tuple[Announcement, PostResult]]


class AnnouncementsCog(
    commands.GroupCog,
    group_name="news",
    group_description="學校公告同步與查詢指令",
):
    def __init__(
        self,
        bot: commands.Bot,
        *,
        database: Database,
        school_news_client: SchoolNewsClient,
        forum_poster: ForumPoster,
        forum_channel_id: int,
        poll_interval_seconds: int,
        dry_run: bool,
    ) -> None:
        self.bot = bot
        self.database = database
        self.school_news_client = school_news_client
        self.forum_poster = forum_poster
        self.forum_channel_id = forum_channel_id
        self.poll_interval_seconds = poll_interval_seconds
        self.dry_run = dry_run
        self.logger = logging.getLogger(__name__)
        self.last_check_at: datetime | None = None
        self.poll_announcements.change_interval(seconds=self.poll_interval_seconds)

    async def cog_load(self) -> None:
        if not self.poll_announcements.is_running():
            self.poll_announcements.start()

    async def cog_unload(self) -> None:
        self.poll_announcements.cancel()

    @tasks.loop(seconds=600)
    async def poll_announcements(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self.sync_announcements(limit=5, include_only_unposted=True, dry_run=False)
        except Exception:
            self.logger.exception("Background announcement polling failed")

    @poll_announcements.before_loop
    async def before_poll_announcements(self) -> None:
        await self.bot.wait_until_ready()

    async def sync_announcements(
        self,
        *,
        limit: int,
        include_only_unposted: bool,
        dry_run: bool,
    ) -> SyncOutcome:
        forum = await self.forum_poster.validate_forum_channel(
            bot=self.bot,
            channel_id=self.forum_channel_id,
        )
        announcements = await self.school_news_client.fetch_latest_announcements(
            limit=limit,
            include_details=True,
        )
        self.last_check_at = datetime.now(UTC)

        scanned = len(announcements)
        new_items = 0
        posted_items = 0
        results: list[tuple[Announcement, PostResult]] = []

        for announcement in announcements:
            is_new = await self.database.save_announcement(announcement)
            stored = await self.database.get_announcement_by_hash(announcement.source_hash)
            if stored is None:
                stored = announcement

            if is_new:
                new_items += 1

            if include_only_unposted and stored.posted_at:
                continue

            post_result = await self.forum_poster.post_announcement(
                forum=forum,
                announcement=stored,
                force_dry_run=dry_run,
            )
            results.append((stored, post_result))

            if post_result.posted and post_result.thread_id is not None:
                posted_items += 1
                await self.database.mark_announcement_posted(
                    stored.source_hash,
                    post_result.thread_id,
                )

        return SyncOutcome(
            scanned=scanned,
            new_items=new_items,
            posted_items=posted_items,
            results=results,
        )

    @app_commands.command(name="latest", description="查詢最近的校內公告")
    @app_commands.describe(
        count="要顯示幾筆公告，預設 5，最多 10",
        category="限定類別",
        unit="限定單位",
        keyword="限定關鍵字",
    )
    async def news_latest(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 10] = 5,
        category: str | None = None,
        unit: str | None = None,
        keyword: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        announcements = await self.database.list_announcements(
            limit=count,
            category=normalize_text(category) or None,
            unit=normalize_text(unit) or None,
            keyword=normalize_text(keyword) or None,
        )

        if not announcements:
            await interaction.followup.send("目前資料庫中沒有符合條件的公告。", ephemeral=True)
            return

        embed = discord.Embed(title="最近公告", color=discord.Color.gold())
        for announcement in announcements:
            value = (
                f"日期：{announcement.date}\n"
                f"類別：{announcement.category}\n"
                f"單位：{announcement.unit}\n"
                f"連結：{announcement.source_url or '無'}"
            )
            embed.add_field(name=announcement.title, value=value, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="search", description="搜尋已保存的公告")
    @app_commands.describe(keyword="搜尋關鍵字")
    async def news_search(
        self,
        interaction: discord.Interaction,
        keyword: app_commands.Range[str, 1, 100],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        announcements = await self.database.search_announcements(keyword, limit=10)
        if not announcements:
            await interaction.followup.send("找不到符合關鍵字的公告。", ephemeral=True)
            return

        embed = discord.Embed(title=f"搜尋結果：{keyword}", color=discord.Color.blurple())
        for announcement in announcements:
            embed.add_field(
                name=announcement.title,
                value=f"{announcement.date} | {announcement.category} | {announcement.unit}\n{announcement.source_url or '無公開連結'}",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="check", description="立即檢查最新公告並同步")
    @admin_only()
    async def news_check(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        outcome = await self.sync_announcements(limit=5, include_only_unposted=True, dry_run=False)
        await interaction.followup.send(self._format_sync_outcome(outcome), ephemeral=True)

    @app_commands.command(name="backfill", description="補發 bot 啟用前的最新公告")
    @admin_only()
    @app_commands.describe(count="要補發幾筆公告，預設 5，最多 30")
    async def news_backfill(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 30] = 5,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        outcome = await self.sync_announcements(limit=count, include_only_unposted=True, dry_run=False)
        await interaction.followup.send(self._format_sync_outcome(outcome), ephemeral=True)

    @app_commands.command(name="dry_run", description="預覽最新公告但不實際發文")
    @admin_only()
    @app_commands.describe(count="要預覽幾筆公告，預設 5，最多 10")
    async def news_dry_run(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 10] = 5,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        outcome = await self.sync_announcements(limit=count, include_only_unposted=False, dry_run=True)
        if not outcome.results:
            await interaction.followup.send("目前沒有可預覽的公告。", ephemeral=True)
            return

        lines = []
        for announcement, result in outcome.results:
            lines.append(
                f"{result.thread_title} | 標籤：{', '.join(result.applied_tag_names) or '無'} | {announcement.source_url or '無公開連結'}"
            )
        await interaction.followup.send("\n".join(lines[:10]), ephemeral=True)

    @app_commands.command(name="status", description="查看公告同步狀態")
    @admin_only()
    async def news_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        total = await self.database.count_announcements()
        last_posted = await self.database.get_last_posted_announcement()
        channel = self.bot.get_channel(self.forum_channel_id)
        embed = discord.Embed(title="公告同步狀態", color=discord.Color.green())
        embed.add_field(
            name="上次檢查時間",
            value=self.last_check_at.isoformat() if self.last_check_at else "尚未檢查",
            inline=False,
        )
        embed.add_field(name="資料庫公告數", value=str(total), inline=True)
        embed.add_field(
            name="論壇頻道",
            value=channel.mention if channel is not None and hasattr(channel, "mention") else str(self.forum_channel_id),
            inline=True,
        )
        if last_posted:
            embed.add_field(
                name="最後發佈公告",
                value=f"{last_posted.title}\n{last_posted.date} | {last_posted.category}",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="sync_tags", description="建立或同步學校公告類別標籤")
    @admin_only()
    async def news_sync_tags(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        forum = await self.forum_poster.validate_forum_channel(
            bot=self.bot,
            channel_id=self.forum_channel_id,
        )
        result = await self.forum_poster.tag_mapper.sync_known_tags(forum)
        lines = [
            f"已對應標籤：{', '.join(result.mapped) or '無'}",
            f"新建立標籤：{', '.join(result.created) or '無'}",
        ]
        if result.missing_permissions:
            lines.append("缺少 Manage Channels 權限，無法自動建立缺少的標籤。")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="tag_map", description="手動指定學校類別對應的論壇標籤")
    @admin_only()
    @app_commands.describe(category="學校公告類別", tag="現有論壇標籤名稱或 ID")
    async def news_tag_map(
        self,
        interaction: discord.Interaction,
        category: str,
        tag: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        forum = await self.forum_poster.validate_forum_channel(
            bot=self.bot,
            channel_id=self.forum_channel_id,
        )
        mapped_tag = await self.forum_poster.tag_mapper.set_manual_mapping(
            category=category,
            forum=forum,
            tag_reference=tag,
        )
        await interaction.followup.send(
            f"已將類別「{category}」對應到論壇標籤「{mapped_tag.name}」。",
            ephemeral=True,
        )

    def _format_sync_outcome(self, outcome: SyncOutcome) -> str:
        lines = [
            f"掃描公告：{outcome.scanned}",
            f"新增資料：{outcome.new_items}",
            f"實際發文：{outcome.posted_items}",
        ]
        if outcome.results:
            latest_titles = [result.thread_title for _, result in outcome.results[:5]]
            lines.append("處理項目：" + "、".join(latest_titles))
        return "\n".join(lines)