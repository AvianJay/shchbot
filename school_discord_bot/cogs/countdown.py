from __future__ import annotations

import asyncio
from datetime import date, datetime, time
import json
import logging

import discord
from discord.ext import commands, tasks

from school_discord_bot.db.database import Database
from school_discord_bot.models.curriculum import TAIPEI_TZ


def parse_exam_date(value: str) -> date:
    """Accept only the YYYY-MM-DD format shown in the slash command."""
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("學測日期格式須為 YYYY-MM-DD，且必須是有效日期。") from None
    if parsed.isoformat() != value:
        raise ValueError("學測日期格式須為 YYYY-MM-DD，且必須是有效日期。")
    return parsed


def countdown_channel_name(exam_date: date, *, now: datetime | None = None) -> str:
    today = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ).date()
    days = max(0, (exam_date - today).days)
    return f"學測倒數 {days} 天"


class CountdownCog(commands.Cog):
    """Persist and refresh a voice-channel countdown at Taipei midnight."""

    def __init__(self, bot: commands.Bot, *, database: Database, guild_id: int) -> None:
        self.bot = bot
        self.database = database
        self.guild_id = guild_id
        self.setting_key = f"gsat_countdown:{guild_id}"
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

    async def _rename_channel(self, channel: discord.VoiceChannel, exam_date: date) -> str:
        name = countdown_channel_name(exam_date)
        if channel.name != name:
            await channel.edit(name=name, reason="更新學測倒數（台灣時間）")
        return name

    async def configure(self, channel: discord.VoiceChannel, exam_date: date) -> str:
        async with self._update_lock:
            channel = self._validate_channel(channel)
            if exam_date < datetime.now(TAIPEI_TZ).date():
                raise ValueError("學測日期不能早於今天（台灣時間）。")
            name = await self._rename_channel(channel, exam_date)
            await self.database.set_setting(
                self.setting_key,
                json.dumps({"channel_id": channel.id, "exam_date": exam_date.isoformat()}),
            )
            return name

    async def update_countdown(self) -> None:
        async with self._update_lock:
            stored = await self.database.get_setting(self.setting_key)
            if stored is None:
                return
            config = json.loads(stored)
            exam_date = parse_exam_date(config["exam_date"])
            channel_id = int(config["channel_id"])
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            channel = self._validate_channel(channel)
            await self._rename_channel(channel, exam_date)

    async def refresh_countdown(self) -> None:
        # Keep the midnight task and reconnect listeners alive if the channel
        # is deleted, permissions change, or a Discord/database request fails.
        try:
            await self.update_countdown()
        except Exception:
            self.logger.exception("Failed to update GSAT countdown in guild %s", self.guild_id)
