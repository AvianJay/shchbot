from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from school_discord_bot.cogs.announcements import admin_only
from school_discord_bot.cogs.countdown import CountdownCog, parse_exam_date
from school_discord_bot.cogs.curriculum import CurriculumCog
from school_discord_bot.cogs.school_links import SchoolLinksView, build_school_links_embed
from school_discord_bot.cogs.verification import VerificationCog
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

    @app_commands.command(name="send_links", description="將學校常用公開連結發送到頻道")
    @admin_only()
    async def school_send_links(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        if not channel:
            await interaction.followup.send("無法取得頻道資訊", ephemeral=True)
            return

        embed = build_school_links_embed()
        view = SchoolLinksView()
        await channel.send(embed=embed, view=view)
        await interaction.followup.send("已將學校常用公開連結發送到頻道", ephemeral=True)

    @app_commands.command(name="help", description="顯示可用指令說明")
    async def school_help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="School Bot 指令說明", color=discord.Color.orange())
        embed.add_field(name="/school setup", value="檢查頻道、權限、資料庫與爬蟲狀態", inline=False)
        embed.add_field(name="/school links", value="顯示學校常用公開入口", inline=False)
        embed.add_field(name="/school send_links", value="將學校常用公開連結發送到頻道", inline=False)
        embed.add_field(name="/school send_curriculum", value="將班級課表查詢面板發送到頻道", inline=False)
        embed.add_field(name="/school countdown <channel> <exam_date>", value="設定學測倒數語音頻道及日期，每天台灣時間 00:00 更新", inline=False)
        embed.add_field(name="/anon setup <public_channel> <review_channel> [require_review]", value="設定匿名版的匿名頻道、後台頻道與是否需要審核", inline=False)
        embed.add_field(name="/anon send_panel", value="將匿名投稿面板發送到頻道", inline=False)
        embed.add_field(name="/anon category add / remove / list", value="管理匿名版分類", inline=False)
        embed.add_field(name="/anon status", value="查看匿名版設定與待審核數量", inline=False)
        embed.add_field(name="/課表 <班級>", value="查詢班級今日課表，例如 /課表 205", inline=False)
        embed.add_field(name="/news check", value="立即同步最新公告", inline=False)
        embed.add_field(name="/news backfill", value="補發 bot 啟用前的公告", inline=False)
        embed.add_field(name="/news status", value="檢查同步狀態", inline=False)
        embed.add_field(name="/news sync_tags", value="建立或同步論壇標籤", inline=False)
        embed.add_field(name="/news tag_map", value="手動設定學校類別對應的論壇標籤", inline=False)
        embed.add_field(name="/news latest", value="查詢最近公告", inline=False)
        embed.add_field(name="/news search", value="搜尋已保存公告", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="countdown", description="設定學測倒數語音頻道，每天台灣時間 00:00 更新名稱")
    @app_commands.describe(channel="顯示學測倒數的語音頻道", exam_date="學測第一天的日期，格式 YYYY-MM-DD")
    @app_commands.guild_only()
    @admin_only()
    async def school_countdown(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        exam_date: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        cog = self.bot.get_cog("CountdownCog")
        if not isinstance(cog, CountdownCog):
            await interaction.followup.send("❌ 學測倒數模組尚未載入", ephemeral=True)
            return
        try:
            target_date = parse_exam_date(exam_date)
            name = await cog.configure(channel, target_date)
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.followup.send("❌ 機器人沒有修改此語音頻道名稱的權限。", ephemeral=True)
            return
        except discord.NotFound:
            await interaction.followup.send("❌ 找不到此語音頻道，請重新選擇。", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send("❌ Discord 暫時無法更新頻道名稱，請稍後重試。", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ 已設定 {channel.mention}，名稱為「{name}」。\n"
            f"學測日期：{target_date.isoformat()}；每天台灣時間 00:00 自動更新。",
            ephemeral=True,
        )

    @school_countdown.error
    async def school_countdown_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "❌ 此指令限具有「管理伺服器」或「管理頻道」權限的管理員使用。",
                ephemeral=True,
            )
            return
        raise error

    @app_commands.command(name="send_curriculum", description="將班級課表查詢面板發送到頻道")
    @admin_only()
    async def school_send_curriculum(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        if not channel or not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("❌ 無法在此頻道發佈", ephemeral=True)
            return

        cog = interaction.client.cogs.get("CurriculumCog")
        if not isinstance(cog, CurriculumCog):
            await interaction.followup.send("❌ 課表模組尚未載入", ephemeral=True)
            return

        await cog.post_panel(channel)
        await interaction.followup.send("✅ 已將班級課表查詢面板發送到頻道", ephemeral=True)

    @app_commands.command(name="send_verification", description="將學生身份驗證面板發送到頻道")
    @admin_only()
    async def school_send_verification(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        if not channel or not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("❌ 無法在此頻道發佈", ephemeral=True)
            return

        cog = interaction.client.cogs.get("VerificationCog")
        if not isinstance(cog, VerificationCog):
            await interaction.followup.send("❌ 驗證模組尚未載入", ephemeral=True)
            return

        await cog.post_panel(channel)
        await interaction.followup.send("✅ 已將學生身份驗證面板發送到頻道", ephemeral=True)

