from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from school_discord_bot.cogs.announcements import admin_only
from school_discord_bot.cogs.school_links import SchoolLinksView, build_school_links_embed
from school_discord_bot.db.database import Database
from school_discord_bot.services.forum_poster import ForumPoster
from school_discord_bot.services.school_news_client import SchoolNewsClient
from school_discord_bot.services.tag_mapper import TagMapper


class AdminCog(
    commands.GroupCog,
    group_name="school",
    group_description="學校網站與實用連結指令",
):
    def __init__(
        self,
        bot: commands.Bot,
        *,
        database: Database,
        school_news_client: SchoolNewsClient,
        forum_poster: ForumPoster,
        tag_mapper: TagMapper,
        guild_id: int,
        forum_channel_id: int,
        dry_run: bool,
    ) -> None:
        self.bot = bot
        self.database = database
        self.school_news_client = school_news_client
        self.forum_poster = forum_poster
        self.tag_mapper = tag_mapper
        self.guild_id = guild_id
        self.forum_channel_id = forum_channel_id
        self.dry_run = dry_run

    @app_commands.command(name="setup", description="檢查 bot、論壇頻道、資料庫與爬蟲狀態")
    @admin_only()
    async def school_setup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.client.get_guild(self.guild_id)
        forum = await self.forum_poster.validate_forum_channel(
            bot=self.bot,
            channel_id=self.forum_channel_id,
        )
        permissions = forum.permissions_for(interaction.guild.me)
        probe = await self.school_news_client.probe()
        total = await self.database.count_announcements()

        embed = discord.Embed(title="School Setup 檢查", color=discord.Color.teal())
        embed.add_field(name="Guild", value=guild.name if guild else str(self.guild_id), inline=False)
        embed.add_field(name="Forum Channel", value=forum.mention, inline=False)
        embed.add_field(name="可發送訊息", value=str(permissions.send_messages), inline=True)
        embed.add_field(name="可建立討論串", value=str(permissions.create_public_threads), inline=True)
        embed.add_field(name="可嵌入連結", value=str(permissions.embed_links), inline=True)
        embed.add_field(name="爬蟲最新標題", value=probe.latest_title or "無資料", inline=False)
        embed.add_field(name="公告總頁數", value=str(probe.total_pages), inline=True)
        embed.add_field(name="Widget UID", value=probe.widget_uid or "未知", inline=True)
        embed.add_field(name="資料庫公告數", value=str(total), inline=True)
        embed.add_field(name="Dry Run", value=str(self.dry_run), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="links", description="顯示學校常用公開連結")
    async def school_links(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=build_school_links_embed(),
            view=SchoolLinksView(),
            ephemeral=True,
        )

    @app_commands.command(name="help", description="顯示可用指令說明")
    async def school_help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="School Bot 指令說明", color=discord.Color.orange())
        embed.add_field(name="/school setup", value="檢查頻道、權限、資料庫與爬蟲狀態", inline=False)
        embed.add_field(name="/school links", value="顯示學校常用公開入口", inline=False)
        embed.add_field(name="/news check", value="立即同步最新公告", inline=False)
        embed.add_field(name="/news backfill", value="補發 bot 啟用前的公告", inline=False)
        embed.add_field(name="/news status", value="檢查同步狀態", inline=False)
        embed.add_field(name="/news sync_tags", value="建立或同步論壇標籤", inline=False)
        embed.add_field(name="/news tag_map", value="手動設定學校類別對應的論壇標籤", inline=False)
        embed.add_field(name="/news latest", value="查詢最近公告", inline=False)
        embed.add_field(name="/news search", value="搜尋已保存公告", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

