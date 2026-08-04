from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta, timezone

import discord
from discord.utils import MISSING
import pytest

from school_discord_bot.models.announcement import Announcement
from school_discord_bot.services.forum_poster import ForumPoster


TAIPEI_TZ = timezone(timedelta(hours=8), name="Asia/Taipei")


@dataclass
class FakeResponse:
    """Minimal stand-in for aiohttp.ClientResponse, enough for HTTPException."""

    status: int = 400
    reason: str = "Bad Request"


@dataclass
class FakeThread:
    id: int


@dataclass
class FakeThreadResult:
    thread: FakeThread


class FakeTagMapper:
    async def resolve_tags_for_category(self, *, forum: object, category: str) -> list[object]:
        return []

    async def resolve_fallback_tags(self, forum: object) -> list[object]:
        return []

    def resolve_existing_unit_tag(self, *, forum: object, unit: str) -> object | None:
        return None


class FakeForum:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.flags = type("Flags", (), {"require_tag": False})()

    async def create_thread(self, **kwargs: object) -> FakeThreadResult:
        self.calls.append(kwargs)
        return FakeThreadResult(thread=FakeThread(id=123))


def test_post_announcement_omits_none_applied_tags() -> None:
    poster = ForumPoster(tag_mapper=FakeTagMapper(), dry_run=False)
    forum = FakeForum()
    announcement = Announcement(
        source_id="1",
        source_hash="hash",
        source_url="https://example.com/news/1",
        title="測試公告",
        date="2026/06/24",
        category="一般公告",
        unit="教學組",
        excerpt="摘要",
    )

    result = asyncio.run(poster.post_announcement(forum=forum, announcement=announcement))

    assert result.posted is True
    assert forum.calls
    assert forum.calls[0]["applied_tags"] is MISSING
    assert forum.calls[0]["name"] == "測試公告"
    assert forum.calls[0]["allowed_mentions"].everyone is False
    assert forum.calls[0]["allowed_mentions"].users is False
    assert forum.calls[0]["allowed_mentions"].roles is False


def test_post_announcement_uses_configured_allowed_mentions() -> None:
    allowed_mentions = discord.AllowedMentions(everyone=False, users=False, roles=False, replied_user=False)
    poster = ForumPoster(
        tag_mapper=FakeTagMapper(),
        dry_run=False,
        allowed_mentions=allowed_mentions,
        announcement_mention_prefix="<@&123>",
    )
    forum = FakeForum()
    announcement = Announcement(
        source_id="1b",
        source_hash="hash-1b",
        source_url="https://example.com/news/1b",
        title="測試 mention 設定",
        date="2026/06/24",
        category="一般公告",
        unit="教學組",
        excerpt="摘要",
    )

    asyncio.run(poster.post_announcement(forum=forum, announcement=announcement))

    assert forum.calls[0]["allowed_mentions"] is allowed_mentions
    assert forum.calls[0]["content"] == "<@&123>\n原始公告：https://example.com/news/1b"


def test_build_thread_title_uses_plain_title_without_category_prefix() -> None:
    poster = ForumPoster(tag_mapper=FakeTagMapper(), dry_run=False)
    announcement = Announcement(
        source_id="2",
        source_hash="hash-2",
        source_url=None,
        title="這是一篇公告",
        date="2026/06/24",
        category="課程活動",
        unit="教學組",
    )

    assert poster.build_thread_title(announcement) == "這是一篇公告"


def test_build_embed_truncates_long_field_values() -> None:
    poster = ForumPoster(tag_mapper=FakeTagMapper(), dry_run=False)
    long_url = "https://example.com/" + ("a" * 1300)
    announcement = Announcement(
        source_id="3",
        source_hash="hash-3",
        source_url="https://example.com/news/3",
        title="測試超長欄位",
        date="2026/06/24",
        category="一般公告",
        unit="教學組",
        excerpt="摘要",
        important_dates=["115/07/01 截止"] * 20,
    )
    announcement.external_links.append(type("Link", (), {"label": "超長連結", "url": long_url})())

    embed = poster.build_embed(announcement)

    assert embed.fields
    for field in embed.fields:
        assert len(field.value) <= 1024


def test_build_embed_does_not_break_attachment_markdown_when_url_is_too_long() -> None:
    poster = ForumPoster(tag_mapper=FakeTagMapper(), dry_run=False)
    long_url = "https://example.com/download?token=" + ("a" * 1400)
    announcement = Announcement(
        source_id="3a",
        source_hash="hash-3a",
        source_url="https://example.com/news/3a",
        title="測試附件過長連結",
        date="2026/06/24",
        category="一般公告",
        unit="教學組",
        excerpt="摘要",
    )
    announcement.attachments.append(type("Attachment", (), {"name": "報名簡章.pdf", "url": long_url})())

    embed = poster.build_embed(announcement)
    attachment_field = next(field for field in embed.fields if field.name == "附件")

    assert attachment_field.value == "- 報名簡章.pdf（連結過長，請見原始公告）"
    assert len(attachment_field.value) <= 1024


def test_build_embed_preserves_newlines_timestamp_and_image() -> None:
    poster = ForumPoster(tag_mapper=FakeTagMapper(), dry_run=False)
    announcement = Announcement(
        source_id="4",
        source_hash="hash-4",
        source_url="https://example.com/news/4",
        title="多行內容公告",
        date="2026/06/24",
        category="課程活動",
        unit="教學組",
        excerpt="短摘要",
        content_html="<p>第一段</p><p>第二段<br>第三行</p><p><img src='https://example.com/image.jpg'></p>",
        raw_payload={"detail_item": {"time": "2026-06-24 12:34:56"}, "root_path": "https://www.dali.tc.edu.tw/ischool/"},
    )

    embed = poster.build_embed(announcement)

    assert embed.description == "第一段\n\n第二段\n第三行"
    assert embed.timestamp == announcement_timestamp(2026, 6, 24, 12, 34, 56)
    assert embed.image.url == "https://example.com/image.jpg"


def test_build_embed_formats_related_links_as_bullets_with_domain_label() -> None:
    poster = ForumPoster(tag_mapper=FakeTagMapper(), dry_run=False)
    announcement = Announcement(
        source_id="5",
        source_hash="hash-5",
        source_url="https://example.com/news/5",
        title="相關連結格式",
        date="2026/06/24",
        category="一般公告",
        unit="教學組",
        excerpt="摘要",
    )
    announcement.external_links.extend(
        [
            type("Link", (), {"label": "https://foo.example.com/path?q=1", "url": "https://foo.example.com/path?q=1"})(),
            type("Link", (), {"label": "報名表", "url": "https://bar.example.com/form"})(),
        ]
    )

    embed = poster.build_embed(announcement)
    related_field = next(field for field in embed.fields if field.name == "相關連結")

    assert related_field.value == "- [foo.example.com](https://foo.example.com/path?q=1)\n- [報名表](https://bar.example.com/form)"


def announcement_timestamp(year: int, month: int, day: int, hour: int, minute: int, second: int):
    from datetime import datetime

    return datetime(year, month, day, hour, minute, second, tzinfo=TAIPEI_TZ)


def test_build_embed_repairs_escaped_source_url() -> None:
    """Regression: an escaped source_url made Discord reject embeds.0.url."""
    poster = ForumPoster(tag_mapper=FakeTagMapper(), dry_run=False)
    announcement = Announcement(
        source_id="20065",
        source_hash="hash-20065",
        source_url="https:\\/\\/www.dali.tc.edu.tw\\/ischool\\/public/news_view/show.php?nid=20065",
        title="學科能力競賽校內初賽報名",
        date="2026/08/04",
        category="競賽資訊",
        unit="設備組",
        excerpt="摘要",
    )

    embed = poster.build_embed(announcement)

    assert embed.url == "https://www.dali.tc.edu.tw/ischool/public/news_view/show.php?nid=20065"
    assert "\\" not in poster.build_initial_message(announcement)


def test_build_embed_omits_url_when_it_cannot_be_repaired() -> None:
    poster = ForumPoster(tag_mapper=FakeTagMapper(), dry_run=False)
    announcement = Announcement(
        source_id="6",
        source_hash="hash-6",
        source_url="/ischool/public/news_view/show.php?nid=6",
        title="沒有可用連結",
        date="2026/08/04",
        category="一般公告",
        unit="教學組",
        excerpt="摘要",
    )

    embed = poster.build_embed(announcement)

    assert "url" not in embed.to_dict()
    assert poster.build_initial_message(announcement) == "原始公告連結：無公開連結"


def test_build_embed_repairs_relative_image_url_against_escaped_root_path() -> None:
    poster = ForumPoster(tag_mapper=FakeTagMapper(), dry_run=False)
    announcement = Announcement(
        source_id="7",
        source_hash="hash-7",
        source_url="https://www.dali.tc.edu.tw/ischool/public/news_view/show.php?nid=7",
        title="相對路徑圖片",
        date="2026/08/04",
        category="一般公告",
        unit="教學組",
        content_html='<p><img src="wr/file/2/7/poster.png"></p>',
        raw_payload={"root_path": "https:\\/\\/www.dali.tc.edu.tw\\/ischool\\/"},
    )

    embed = poster.build_embed(announcement)

    assert embed.image.url == "https://www.dali.tc.edu.tw/ischool/wr/file/2/7/poster.png"


def test_build_embed_skips_link_entries_with_unusable_urls() -> None:
    poster = ForumPoster(tag_mapper=FakeTagMapper(), dry_run=False)
    announcement = Announcement(
        source_id="8",
        source_hash="hash-8",
        source_url="https://example.com/news/8",
        title="連結過濾",
        date="2026/08/04",
        category="一般公告",
        unit="教學組",
    )
    announcement.external_links.extend(
        [
            type("Link", (), {"label": "壞連結", "url": "/relative/only"})(),
            type("Link", (), {"label": "報名表", "url": "https://bar.example.com/form"})(),
        ]
    )

    embed = poster.build_embed(announcement)
    related_field = next(field for field in embed.fields if field.name == "相關連結")

    assert related_field.value == "- [報名表](https://bar.example.com/form)"


def test_post_announcement_does_not_retry_non_tag_errors() -> None:
    """A malformed-embed 400 fails identically on retry, so it must not be retried."""

    class RejectingForum(FakeForum):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def create_thread(self, **kwargs: object) -> FakeThreadResult:
            self.attempts += 1
            raise discord.HTTPException(
                FakeResponse(),
                {"code": 50035, "message": "Invalid Form Body", "errors": {}},
            )

    poster = ForumPoster(tag_mapper=FakeTagMapper(), dry_run=False)
    forum = RejectingForum()
    announcement = Announcement(
        source_id="9",
        source_hash="hash-9",
        source_url="https://example.com/news/9",
        title="不該重試",
        date="2026/08/04",
        category="一般公告",
        unit="教學組",
    )

    with pytest.raises(discord.HTTPException):
        asyncio.run(poster.post_announcement(forum=forum, announcement=announcement))

    assert forum.attempts == 1


def test_post_announcement_still_retries_when_a_tag_is_required() -> None:
    class TagRequiringForum(FakeForum):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def create_thread(self, **kwargs: object) -> FakeThreadResult:
            self.attempts += 1
            if not kwargs.get("applied_tags") or kwargs["applied_tags"] is MISSING:
                raise discord.HTTPException(
                    FakeResponse(),
                    {
                        "code": 40067,
                        "message": "A tag is required to create a forum post in this channel",
                    },
                )
            return FakeThreadResult(thread=FakeThread(id=999))

    class FallbackTagMapper(FakeTagMapper):
        async def resolve_fallback_tags(self, forum: object) -> list[object]:
            return [type("Tag", (), {"id": 1, "name": "一般公告"})()]

    poster = ForumPoster(tag_mapper=FallbackTagMapper(), dry_run=False)
    forum = TagRequiringForum()
    announcement = Announcement(
        source_id="10",
        source_hash="hash-10",
        source_url="https://example.com/news/10",
        title="需要標籤",
        date="2026/08/04",
        category="不存在的類別",
        unit="教學組",
    )

    result = asyncio.run(poster.post_announcement(forum=forum, announcement=announcement))

    assert forum.attempts == 2
    assert result.posted is True
    assert result.thread_id == 999
    assert result.applied_tag_names == ["一般公告"]