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
from school_discord_bot.cogs.countdown import CountdownCog, countdown_channel_name
from school_discord_bot.db.database import Database
from school_discord_bot.models.curriculum import TAIPEI_TZ
from school_discord_bot.models.exam import ExamType
from school_discord_bot.services.exam_calendar_client import ExamCalendarError


NOW = datetime(2027, 1, 1, 12, tzinfo=TAIPEI_TZ)
EXAM_DATES = {ExamType.GSAT: date(2027, 1, 22), ExamType.SUBJECT: date(2027, 7, 10)}


def _voice_channel(channel_id: int):
    voice = MagicMock(spec=discord.VoiceChannel)
    voice.id = channel_id
    voice.guild = SimpleNamespace(id=456, me=object())
    voice.name = "原本的語音頻道"
    voice.mention = f"<#{channel_id}>"
    voice.permissions_for.return_value = discord.Permissions(view_channel=True, manage_channels=True)

    async def edit(**kwargs):
        voice.name = kwargs["name"]

    voice.edit = AsyncMock(side_effect=edit)
    return voice


@pytest.fixture
def channel():
    return _voice_channel(123)


@pytest.fixture
def subject_channel():
    return _voice_channel(124)


@pytest.fixture
def bot(channel, subject_channel):
    return SimpleNamespace(
        get_channel=Mock(side_effect={123: channel, 124: subject_channel}.get),
        fetch_channel=AsyncMock(return_value=channel),
        wait_until_ready=AsyncMock(),
    )


@pytest.fixture
def calendar_client():
    return SimpleNamespace(
        refresh=AsyncMock(),
        get_exam_date=AsyncMock(side_effect=lambda exam_type, **kwargs: EXAM_DATES[exam_type]),
    )


@pytest.fixture(autouse=True)
def freeze_clock(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr(countdown_module, "datetime", FrozenDateTime)


def _cog(bot, database, calendar_client):
    return CountdownCog(bot, database=database, guild_id=456, calendar_client=calendar_client)


def _database(configs=None):
    return SimpleNamespace(
        get_setting=AsyncMock(side_effect=(configs or {}).get),
        set_setting=AsyncMock(),
    )


def _admin(bot, database):
    return AdminCog(
        bot, database=database, school_news_client=Mock(), forum_poster=Mock(),
        tag_mapper=Mock(), guild_id=456, forum_channel_id=789, dry_run=False,
    )


@pytest.mark.parametrize("exam_type", list(ExamType))
@pytest.mark.parametrize(
    ("now", "days"),
    [
        (datetime(2027, 1, 20, 15, 59, 59, tzinfo=UTC), 2),
        (datetime(2027, 1, 20, 16, 0, tzinfo=UTC), 1),
        (datetime(2027, 1, 21, 16, 0, tzinfo=UTC), 0),
        (datetime(2027, 1, 24, 16, 0, tzinfo=UTC), 0),
    ],
)
def test_countdown_changes_at_taipei_midnight(now: datetime, days: int, exam_type: ExamType) -> None:
    assert countdown_channel_name(EXAM_DATES[ExamType.GSAT], now=now, exam_type=exam_type) == f"{exam_type}倒數 {days} 天"


def test_next_update_is_taipei_midnight(bot, calendar_client) -> None:
    cog = _cog(bot, _database(), calendar_client)
    next_update = cog.daily_countdown._get_next_sleep_time(datetime(2027, 1, 1, 15, 59, tzinfo=UTC))
    assert next_update.astimezone(UTC) == datetime(2027, 1, 1, 16, 0, tzinfo=UTC)


def test_configure_persists_both_countdowns_and_restart_refreshes(
    tmp_path: Path, bot, channel, subject_channel, calendar_client
) -> None:
    async def run() -> None:
        database = Database(tmp_path / "countdowns.sqlite3")
        await database.initialize()
        try:
            cog = _cog(bot, database, calendar_client)
            assert await cog.configure(channel) == ("學測倒數 21 天", EXAM_DATES[ExamType.GSAT])
            gsat_config = await database.get_setting("gsat_countdown:456")
            configured, name, target_date = await cog.configure_subject(subject_channel.guild, subject_channel)
            assert configured is subject_channel
            assert (name, target_date) == ("分科倒數 190 天", EXAM_DATES[ExamType.SUBJECT])
            assert await database.get_setting("gsat_countdown:456") == gsat_config
            await database.close()
            await database.initialize()

            channel.name = subject_channel.name = "等待更新"
            calendar_client.refresh.reset_mock()
            restarted = _cog(bot, database, calendar_client)
            await restarted.on_ready()
            calendar_client.refresh.assert_awaited_once()
            assert channel.name == "學測倒數 21 天"
            assert subject_channel.name == "分科倒數 190 天"
            channel.edit.reset_mock()
            subject_channel.edit.reset_mock()
            await restarted.on_resumed()
            channel.edit.assert_not_awaited()
            subject_channel.edit.assert_not_awaited()
        finally:
            await database.close()

    asyncio.run(run())


def test_daily_refresh_replaces_legacy_manual_date(bot, channel, calendar_client) -> None:
    async def run() -> None:
        database = _database({"gsat_countdown:456": json.dumps({"channel_id": 123, "exam_date": "2028-01-01"})})
        cog = _cog(bot, database, calendar_client)
        await cog.daily_countdown()
        bot.wait_until_ready.assert_awaited_once()
        calendar_client.refresh.assert_awaited_once()
        assert channel.name == "學測倒數 21 天"
        assert database.set_setting.await_args.args[0] == "gsat_countdown:456"
        assert json.loads(database.set_setting.await_args.args[1]) == {"channel_id": 123, "exam_date": "2027-01-22"}

    asyncio.run(run())


def test_fetches_uncached_channel_and_clamps_past_date(bot, channel, calendar_client) -> None:
    async def run() -> None:
        database = _database({"gsat_countdown:456": json.dumps({"channel_id": 123, "exam_date": "2026-12-31"})})
        bot.get_channel.side_effect = None
        bot.get_channel.return_value = None
        calendar_client.get_exam_date.side_effect = None
        calendar_client.get_exam_date.return_value = date(2026, 12, 31)
        await _cog(bot, database, calendar_client).daily_countdown()
        bot.fetch_channel.assert_awaited_once_with(123)
        assert channel.name == "學測倒數 0 天"

    asyncio.run(run())


def test_unconfigured_countdowns_do_not_fetch_calendar(bot, channel, calendar_client) -> None:
    async def run() -> None:
        await _cog(bot, _database(), calendar_client).daily_countdown()
        calendar_client.refresh.assert_not_awaited()
        calendar_client.get_exam_date.assert_not_awaited()
        bot.get_channel.assert_not_called()
        channel.edit.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("problem", ["permissions", "guild", "type", "missing_date", "forbidden"])
def test_failed_setup_preserves_configuration(problem: str, bot, channel, calendar_client) -> None:
    async def run() -> None:
        database = _database()
        target = channel
        expected_error = ValueError
        if problem == "permissions":
            channel.permissions_for.return_value = discord.Permissions(view_channel=True)
        elif problem == "guild":
            channel.guild.id = 999
        elif problem == "type":
            target = MagicMock(spec=discord.TextChannel)
        elif problem == "missing_date":
            calendar_client.get_exam_date.side_effect = ExamCalendarError("來源沒有考試日期")
        elif problem == "forbidden":
            expected_error = discord.Forbidden
            channel.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "")
        with pytest.raises(expected_error):
            await _cog(bot, database, calendar_client).configure(target)
        database.set_setting.assert_not_awaited()
        if problem != "forbidden":
            channel.edit.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("exam_type", list(ExamType))
def test_countdowns_cannot_share_a_channel(tmp_path: Path, bot, channel, exam_type: ExamType, calendar_client) -> None:
    async def run() -> None:
        database = Database(tmp_path / "countdowns.sqlite3")
        await database.initialize()
        try:
            cog = _cog(bot, database, calendar_client)
            other_type = next(kind for kind in ExamType if kind != exam_type)
            original_name, _ = await cog.configure(channel, exam_type=other_type)
            original_config = await database.get_setting(cog.setting_keys[other_type])
            channel.edit.reset_mock()
            with pytest.raises(ValueError, match="不同的語音頻道"):
                await cog.configure(channel, exam_type=exam_type)
            channel.edit.assert_not_awaited()
            assert channel.name == original_name
            assert await database.get_setting(cog.setting_keys[other_type]) == original_config
            assert await database.get_setting(cog.setting_keys[exam_type]) is None
        finally:
            await database.close()

    asyncio.run(run())


@pytest.mark.parametrize("broken", ["deleted", "malformed", "missing_date"])
def test_failed_gsat_does_not_block_subject_update(bot, subject_channel, calendar_client, broken: str) -> None:
    async def run() -> None:
        database = _database({
            "gsat_countdown:456": "invalid JSON" if broken == "malformed" else json.dumps({"channel_id": 123}),
            "subject_countdown:456": json.dumps({"channel_id": 124}),
        })
        if broken == "deleted":
            bot.get_channel.side_effect = {124: subject_channel}.get
            bot.fetch_channel.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "")
        elif broken == "missing_date":
            async def resolve(exam_type, **kwargs):
                if exam_type == ExamType.GSAT:
                    raise ExamCalendarError("缺少學測日期")
                return EXAM_DATES[exam_type]
            calendar_client.get_exam_date.side_effect = resolve
        await _cog(bot, database, calendar_client).daily_countdown()
        calendar_client.refresh.assert_awaited_once()
        assert subject_channel.name == "分科倒數 190 天"

    asyncio.run(run())


def test_calendar_failure_without_cache_preserves_channel_name(bot, channel, calendar_client, caplog) -> None:
    async def run() -> None:
        database = _database({"gsat_countdown:456": json.dumps({"channel_id": 123})})
        calendar_client.refresh.side_effect = ExamCalendarError("沒有可用快取")
        await _cog(bot, database, calendar_client).refresh_countdown()
        channel.edit.assert_not_awaited()
        database.set_setting.assert_not_awaited()
        assert "Failed to refresh exam calendar" in caplog.text

    asyncio.run(run())


@pytest.mark.parametrize("command_name", ["school_countdown", "school_subject_countdown"])
@pytest.mark.parametrize(
    ("manage_guild", "manage_channels", "allowed"),
    [(False, False, False), (True, False, True), (False, True, True)],
)
def test_only_administrators_can_configure(manage_guild, manage_channels, allowed, command_name) -> None:
    interaction = SimpleNamespace(
        user=SimpleNamespace(guild_permissions=discord.Permissions(manage_guild=manage_guild, manage_channels=manage_channels))
    )
    command = getattr(AdminCog, command_name)
    assert command.guild_only
    assert [p.name for p in command.parameters] == ["channel"]
    assert command.parameters[0].channel_types == [discord.ChannelType.voice]
    assert asyncio.run(command.checks[0](interaction)) is allowed


@pytest.mark.parametrize("exam_type", list(ExamType))
@pytest.mark.parametrize("source_available", [True, False])
def test_setup_command_uses_source_date_and_reports_privately(exam_type, source_available, bot, channel, calendar_client) -> None:
    async def run() -> None:
        database = _database()
        if not source_available:
            calendar_client.refresh.side_effect = ExamCalendarError("暫時無法取得考試日曆")
        bot.get_cog = Mock(return_value=_cog(bot, database, calendar_client))
        interaction = SimpleNamespace(
            guild=channel.guild,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        admin = _admin(bot, database)
        command = AdminCog.school_countdown if exam_type == ExamType.GSAT else AdminCog.school_subject_countdown
        await command.callback(admin, interaction, channel)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        message = interaction.followup.send.await_args.args[0]
        assert interaction.followup.send.await_args.kwargs["ephemeral"]
        if source_available:
            assert EXAM_DATES[exam_type].isoformat() in message
            assert "00:00" in message
            assert database.set_setting.await_args.args[0] == ("gsat_countdown:456" if exam_type == ExamType.GSAT else "subject_countdown:456")
        else:
            assert "暫時無法取得考試日曆" in message
            database.set_setting.assert_not_awaited()
            channel.edit.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("source_available", [True, False])
def test_subject_command_creates_channel_after_resolving_date(bot, subject_channel, calendar_client, source_available) -> None:
    async def run() -> None:
        async def create(name, **kwargs):
            subject_channel.name = name
            return subject_channel
        guild = subject_channel.guild
        guild.create_voice_channel = AsyncMock(side_effect=create)
        if not source_available:
            calendar_client.refresh.side_effect = ExamCalendarError("暫時無法取得考試日曆")
        database = _database()
        bot.get_cog = Mock(return_value=_cog(bot, database, calendar_client))
        interaction = SimpleNamespace(
            guild=guild, response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        command = AdminCog.school_subject_countdown
        assert not command.parameters[0].required
        await command.callback(_admin(bot, database), interaction)
        if source_available:
            guild.create_voice_channel.assert_awaited_once_with("分科倒數 190 天", reason="新增分科倒數語音頻道")
            calendar_client.refresh.assert_awaited_once()
            assert json.loads(database.set_setting.await_args.args[1])["channel_id"] == 124
        else:
            guild.create_voice_channel.assert_not_awaited()
            database.set_setting.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("existing_channel", [False, True])
def test_failed_setup_removes_only_a_newly_created_channel(bot, subject_channel, calendar_client, existing_channel) -> None:
    async def run() -> None:
        guild = subject_channel.guild
        guild.create_voice_channel = AsyncMock(return_value=subject_channel)
        database = _database()
        database.set_setting.side_effect = RuntimeError("Database unavailable")
        with pytest.raises(RuntimeError, match="Database unavailable"):
            await _cog(bot, database, calendar_client).configure_subject(guild, subject_channel if existing_channel else None)
        if existing_channel:
            guild.create_voice_channel.assert_not_awaited()
            subject_channel.delete.assert_not_awaited()
        else:
            guild.create_voice_channel.assert_awaited_once()
            subject_channel.delete.assert_awaited_once()

    asyncio.run(run())
