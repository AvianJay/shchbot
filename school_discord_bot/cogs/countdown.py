from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from enum import StrEnum
import json
import logging

import discord
from discord.ext import commands, tasks

from school_discord_bot.db.database import Database
from school_discord_bot.models.curriculum import TAIPEI_TZ


class ExamType(StrEnum):
    GSAT = "學測"
    SUBJECT = "分科"


def parse_exam_date(value: str, *, exam_type: ExamType = ExamType.GSAT) -> date:
    """Accept only the YYYY-MM-DD format shown in the slash command."""
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{exam_type}日期格式須為 YYYY-MM-DD，且必須是有效日期。") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{exam_type}日期格式須為 YYYY-MM-DD，且必須是有效日期。")
    return parsed


def countdown_channel_name(
    exam_date: date, *, now: datetime | None = None, exam_type: ExamType = ExamType.GSAT
) -> str:
    today = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ).date()
    days = max(0, (exam_date - today).days)
    return f"{exam_type}倒數 {days} 天"


class CountdownCog(commands.Cog):
    """Persist and refresh separate exam countdowns at Taipei midnight."""

    def __init__(self, bot: commands.Bot, *, database: Database, guild_id: int) -> None:
        self.bot = bot
        self.database = database
        self.guild_id = guild_id
        self.setting_key = f"gsat_countdown:{guild_id}"
        self.setting_keys = {
            ExamType.GSAT: self.setting_key,
            ExamType.SUBJECT: f"subject_countdown:{guild_id}",
        }
        self.logger = logging.getLogger(__name__)
        self._update_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.daily_countdown.start()

    async def cog_unload(self) -> None:
        self.daily_countdown.cancel()

    @tasks.loop(time=time(0, 0, tzinfo=TAIPEI_TZ))
    async def daily_countdown(self) -> None:
        await self.bot.wait_until_ready()
        await self.refresh_countdown()

    @daily_countdown.before_loop
    async def before_daily_countdown(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.refresh_countdown()

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        await self.refresh_countdown()

    def _validate_channel(self, channel: object) -> discord.VoiceChannel:
        if not isinstance(channel, discord.VoiceChannel):
            raise ValueError("請選擇語音頻道。")
        if channel.guild.id != self.guild_id:
            raise ValueError("請選擇此伺服器的語音頻道。")
        member = channel.guild.me
        if member is None:
            raise ValueError("無法取得機器人的伺服器成員資料。")
        permissions = channel.permissions_for(member)
        if not permissions.view_channel or not permissions.manage_channels:
            raise ValueError("機器人需要該語音頻道的「檢視頻道」及「管理頻道」權限。")
        return channel

    def _validate_exam_date(self, exam_date: date, exam_type: ExamType) -> None:
        if exam_date < datetime.now(TAIPEI_TZ).date():
            raise ValueError(f"{exam_type}日期不能早於今天（台灣時間）。")

    async def _rename_channel(
        self, channel: discord.VoiceChannel, exam_date: date, exam_type: ExamType
    ) -> str:
        name = countdown_channel_name(exam_date, exam_type=exam_type)
        if channel.name != name:
            await channel.edit(name=name, reason=f"更新{exam_type}倒數（台灣時間）")
        return name

    async def configure(
        self, channel: discord.VoiceChannel, exam_date: date, *, exam_type: ExamType = ExamType.GSAT
    ) -> str:
        async with self._update_lock:
            channel = self._validate_channel(channel)
            self._validate_exam_date(exam_date, exam_type)
            for other_type, setting_key in self.setting_keys.items():
                if other_type == exam_type:
                    continue
                other_config = await self.database.get_setting(setting_key)
                if other_config is not None and int(json.loads(other_config)["channel_id"]) == channel.id:
                    raise ValueError("學測與分科倒數須使用不同的語音頻道。")
            name = await self._rename_channel(channel, exam_date, exam_type)
            await self.database.set_setting(
                self.setting_keys[exam_type],
                json.dumps({"channel_id": channel.id, "exam_date": exam_date.isoformat()}),
            )
            return name

    async def configure_subject(
        self, guild: discord.Guild, exam_date: date, channel: discord.VoiceChannel | None = None
    ) -> tuple[discord.VoiceChannel, str]:
        if guild.id != self.guild_id:
            raise ValueError("請在已設定的伺服器使用此指令。")
        self._validate_exam_date(exam_date, ExamType.SUBJECT)
        created = channel is None
        if channel is None:
            channel = await guild.create_voice_channel(
                countdown_channel_name(exam_date, exam_type=ExamType.SUBJECT),
                reason="新增分科倒數語音頻道",
            )
        try:
            name = await self.configure(channel, exam_date, exam_type=ExamType.SUBJECT)
        except Exception:
            if created:
                try:
                    await channel.delete(reason="分科倒數設定失敗，移除新建頻道")
                except discord.HTTPException:
                    self.logger.exception("Failed to remove newly created countdown channel %s", channel.id)
            raise
        return channel, name

    async def update_countdown(self, exam_type: ExamType = ExamType.GSAT) -> None:
        async with self._update_lock:
            stored = await self.database.get_setting(self.setting_keys[exam_type])
            if stored is None:
                return
            config = json.loads(stored)
            exam_date = parse_exam_date(config["exam_date"], exam_type=exam_type)
            channel_id = int(config["channel_id"])
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            channel = self._validate_channel(channel)
            await self._rename_channel(channel, exam_date, exam_type)

    async def refresh_countdown(self) -> None:
        # Keep the midnight task and reconnect listeners alive if the channel
        # is deleted, permissions change, or a Discord/database request fails.
        for exam_type in ExamType:
            try:
                await self.update_countdown(exam_type)
            except Exception:
                self.logger.exception("Failed to update %s countdown in guild %s", exam_type.name, self.guild_id)
