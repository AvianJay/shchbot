from __future__ import annotations

from school_discord_bot.config import Settings


def test_settings_parse_announcement_allowed_mentions(monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GUILD_ID", "1")
    monkeypatch.setenv("ANNOUNCEMENT_FORUM_CHANNEL_ID", "2")
    monkeypatch.setenv("ANNOUNCEMENT_MENTION_EVERYONE", "false")
    monkeypatch.setenv("ANNOUNCEMENT_MENTION_USERS", "true")
    monkeypatch.setenv("ANNOUNCEMENT_MENTION_ROLE_IDS", "123, 456")

    settings = Settings.from_env()

    assert settings.announcement_allowed_mentions.everyone is False
    assert settings.announcement_allowed_mentions.users is True
    assert settings.announcement_allowed_mentions.roles == [123, 456]
    assert settings.announcement_allowed_mentions.replied_user is False
    assert settings.announcement_mention_prefix == "<@&123> <@&456>"


def test_settings_parse_custom_announcement_mention_text(monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GUILD_ID", "1")
    monkeypatch.setenv("ANNOUNCEMENT_FORUM_CHANNEL_ID", "2")
    monkeypatch.setenv("ANNOUNCEMENT_MENTION_EVERYONE", "true")
    monkeypatch.setenv("ANNOUNCEMENT_MENTION_ROLE_IDS", "123")
    monkeypatch.setenv("ANNOUNCEMENT_MENTION_TEXT", "新公告來了 <@&999>")

    settings = Settings.from_env()

    assert settings.announcement_mention_prefix == "新公告來了 <@&999>"