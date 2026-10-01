from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import pytest

from school_discord_bot.cogs import countdown as countdown_module
from school_discord_bot.cogs.admin import AdminCog
from school_discord_bot.cogs.countdown import CountdownCog, countdown_channel_name, parse_exam_date
from school_discord_bot.db.database import Database
from school_discord_bot.models.curriculum import TAIPEI_TZ


NOW = datetime(2027, 1, 1, 12, tzinfo=TAIPEI_TZ)
EXAM_DATE = date(2027, 1, 17)  # Synthetic test date, not an official exam schedule.


@pytest.fixture
def channel():
    voice = MagicMock(spec=discord.VoiceChannel)
    voice.id = 123
    voice.guild = SimpleNamespace(id=456, me=object())
    voice.name = "原本的語音頻道"
    voice.mention = "<#123>"
    voice.permissions_for.return_value = discord.Permissions(view_channel=True, manage_channels=True)

    async def edit(**kwargs):
        voice.name = kwargs["name"]

    voice.edit = AsyncMock(side_effect=edit)
    return voice


@pytest.fixture
def bot(channel):
    return SimpleNamespace(
        get_channel=Mock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
        wait_until_ready=AsyncMock(),
    )


@pytest.fixture(autouse=True)
def freeze_clock(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr(countdown_module, "datetime", FrozenDateTime)


@pytest.mark.parametrize("value", ["2027-02-30", "20270117", "2027-W02-7", "2027/01/17", " 2027-01-17", ""])
def test_invalid_exam_date_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_exam_date(value)


def test_valid_exam_date() -> None:
    assert parse_exam_date("2028-02-29") == date(2028, 2, 29)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2027, 1, 15, 15, 59, 59, tzinfo=UTC), "學測倒數 2 天"),
        (datetime(2027, 1, 15, 16, 0, tzinfo=UTC), "學測倒數 1 天"),
        (datetime(2027, 1, 16, 16, 0, tzinfo=UTC), "學測倒數 0 天"),
        (datetime(2027, 1, 18, 16, 0, tzinfo=UTC), "學測倒數 0 天"),
    ],
)
def test_countdown_changes_at_taipei_midnight(now: datetime, expected: str) -> None:
    assert countdown_channel_name(EXAM_DATE, now=now) == expected


def test_next_update_is_taipei_midnight(bot) -> None:
    cog = CountdownCog(bot, database=Mock(), guild_id=456)
    # UTC 15:59 is 23:59 in Taipei; the scheduled update must be one minute away.
    next_update = cog.daily_countdown._get_next_sleep_time(datetime(2027, 1, 1, 15, 59, tzinfo=UTC))
    assert next_update.astimezone(UTC) == datetime(2027, 1, 1, 16, 0, tzinfo=UTC)


def test_configure_persists_and_restart_refreshes(tmp_path: Path, bot, channel) -> None:
    async def run() -> None:
        path = tmp_path / "countdown.sqlite3"
        database = Database(path)
        await database.initialize()
        try:
            cog = CountdownCog(bot, database=database, guild_id=456)
            assert await cog.configure(channel, EXAM_DATE) == "學測倒數 16 天"
            stored = json.loads(await database.get_setting(cog.setting_key))
            assert stored == {"channel_id": 123, "exam_date": "2027-01-17"}
        finally:
            await database.close()

        restarted_database = Database(path)
        await restarted_database.initialize()
        try:
            restarted = CountdownCog(bot, database=restarted_database, guild_id=456)
            channel.name = "學測倒數 17 天"
            channel.edit.reset_mock()
            await restarted.on_ready()
            assert channel.name == "學測倒數 16 天"
            channel.edit.assert_awaited_once()
            channel.edit.reset_mock()
            await restarted.on_resumed()
            channel.edit.assert_not_awaited()
        finally:
            await restarted_database.close()

    asyncio.run(run())


def test_daily_refresh_fetches_uncached_channel_and_clamps_past_date(bot, channel) -> None:
    async def run() -> None:
        database = SimpleNamespace(
            get_setting=AsyncMock(return_value=json.dumps({"channel_id": 123, "exam_date": "2026-12-31"}))
        )
        bot.get_channel.return_value = None
        cog = CountdownCog(bot, database=database, guild_id=456)
        await cog.daily_countdown()
        bot.wait_until_ready.assert_awaited_once()
        bot.fetch_channel.assert_awaited_once_with(123)
        assert channel.name == "學測倒數 0 天"

    asyncio.run(run())


def test_unconfigured_countdown_does_nothing(bot, channel) -> None:
    async def run() -> None:
        database = SimpleNamespace(get_setting=AsyncMock(return_value=None))
        cog = CountdownCog(bot, database=database, guild_id=456)
        await cog.update_countdown()
        bot.get_channel.assert_not_called()
        bot.fetch_channel.assert_not_awaited()
        channel.edit.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("problem", ["permissions", "guild", "type", "past_date", "forbidden"])
def test_failed_setup_preserves_previous_configuration(problem: str, bot, channel) -> None:
    async def run() -> None:
        database = SimpleNamespace(set_setting=AsyncMock())
        cog = CountdownCog(bot, database=database, guild_id=456)
        target = channel
        target_date = EXAM_DATE
        expected_error = ValueError
        if problem == "permissions":
            channel.permissions_for.return_value = discord.Permissions(view_channel=True)
        elif problem == "guild":
            channel.guild.id = 999
        elif problem == "type":
            target = MagicMock(spec=discord.TextChannel)
        elif problem == "past_date":
            target_date = date(2026, 12, 31)
        elif problem == "forbidden":
            expected_error = discord.Forbidden
            channel.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "")
        with pytest.raises(expected_error):
            await cog.configure(target, target_date)
        database.set_setting.assert_not_awaited()
        if problem != "forbidden":
            channel.edit.assert_not_awaited()

    asyncio.run(run())


def test_deleted_channel_does_not_prevent_later_refresh(bot, channel, caplog) -> None:
    async def run() -> None:
        database = SimpleNamespace(
            get_setting=AsyncMock(return_value=json.dumps({"channel_id": 123, "exam_date": "2027-01-17"}))
        )
        bot.get_channel.return_value = None
        bot.fetch_channel.side_effect = [
            discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), ""),
            channel,
        ]
        cog = CountdownCog(bot, database=database, guild_id=456)
        await cog.refresh_countdown()
        assert "Failed to update GSAT countdown" in caplog.text
        await cog.refresh_countdown()
        assert channel.name == "學測倒數 16 天"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("manage_guild", "manage_channels", "allowed"),
    [(False, False, False), (True, False, True), (False, True, True)],
)
def test_only_administrators_can_configure(manage_guild: bool, manage_channels: bool, allowed: bool) -> None:
    interaction = SimpleNamespace(
        user=SimpleNamespace(guild_permissions=discord.Permissions(manage_guild=manage_guild, manage_channels=manage_channels))
    )
    command = AdminCog.school_countdown
    assert command.guild_only
    assert command.parameters[0].channel_types == [discord.ChannelType.voice]
    assert asyncio.run(command.checks[0](interaction)) is allowed


@pytest.mark.parametrize("exam_date", ["2027-01-17", "2027/01/17"])
def test_setup_command_reports_result_privately(exam_date: str, bot, channel) -> None:
    async def run() -> None:
        database = SimpleNamespace(set_setting=AsyncMock())
        countdown = CountdownCog(bot, database=database, guild_id=456)
        bot.get_cog = Mock(return_value=countdown)
        admin = AdminCog(
            bot, database=database, school_news_client=Mock(), forum_poster=Mock(),
            tag_mapper=Mock(), guild_id=456, forum_channel_id=789, dry_run=False,
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await AdminCog.school_countdown.callback(admin, interaction, channel, exam_date)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        message = interaction.followup.send.await_args.args[0]
        assert interaction.followup.send.await_args.kwargs["ephemeral"]
        if exam_date == "2027-01-17":
            assert "學測倒數 16 天" in message
            assert "00:00" in message
            database.set_setting.assert_awaited_once()
        else:
            assert "YYYY-MM-DD" in message
            database.set_setting.assert_not_awaited()
            channel.edit.assert_not_awaited()

    asyncio.run(run())
