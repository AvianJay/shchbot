from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import discord
from discord.utils import MISSING

from school_discord_bot.models.announcement import Announcement, normalize_text
from school_discord_bot.services.tag_mapper import TagMapper


@dataclass(slots=True)
class PostResult:
    thread_id: int | None
    thread_title: str
    applied_tag_names: list[str]
    posted: bool


class ForumPoster:
    """Create Discord forum posts for school announcements with safe defaults."""

    FIELD_VALUE_LIMIT = 1024

    def __init__(
        self,
        *,
        tag_mapper: TagMapper,
        dry_run: bool,
        logger: logging.Logger | None = None,
    ) -> None:
        self.tag_mapper = tag_mapper
        self.dry_run = dry_run
        self.logger = logger or logging.getLogger(__name__)

    async def validate_forum_channel(
        self,
        *,
        bot: discord.Client,
        channel_id: int,
    ) -> discord.ForumChannel:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.ForumChannel):
            raise TypeError("configured announcement channel is not a Discord forum channel")
        return channel

    async def post_announcement(
        self,
        *,
        forum: discord.ForumChannel,
        announcement: Announcement,
        force_dry_run: bool = False,
    ) -> PostResult:
        title = self.build_thread_title(announcement)
        embed = self.build_embed(announcement)
        category_tags = await self.tag_mapper.resolve_tags_for_category(
            forum=forum,
            category=announcement.category,
        )
        unit_tag = self.tag_mapper.resolve_existing_unit_tag(forum=forum, unit=announcement.unit)
        applied_tags = self._merge_tags(category_tags, [unit_tag] if unit_tag else [])

        if self._forum_requires_tag(forum) and not applied_tags:
            applied_tags = await self.tag_mapper.resolve_fallback_tags(forum)

        applied_tag_names = [tag.name for tag in applied_tags]
        should_post = not (self.dry_run or force_dry_run)
        if not should_post:
            return PostResult(
                thread_id=None,
                thread_title=title,
                applied_tag_names=applied_tag_names,
                posted=False,
            )

        content = self.build_initial_message(announcement)
        create_thread_kwargs = {
            "name": title,
            "content": content,
            "embed": embed,
            "allowed_mentions": discord.AllowedMentions.none(),
            "applied_tags": applied_tags if applied_tags else MISSING,
        }
        try:
            thread = await forum.create_thread(**create_thread_kwargs)
        except discord.HTTPException as exc:
            if not applied_tags:
                fallback_tags = await self.tag_mapper.resolve_fallback_tags(forum)
                fallback_kwargs = dict(create_thread_kwargs)
                fallback_kwargs["applied_tags"] = fallback_tags if fallback_tags else MISSING
                thread = await forum.create_thread(**fallback_kwargs)
                applied_tag_names = [tag.name for tag in fallback_tags]
            else:
                self.logger.exception("Failed to post announcement %s", announcement.source_id)
                raise exc

        thread_id = getattr(thread.thread, "id", None)
        return PostResult(
            thread_id=thread_id,
            thread_title=title,
            applied_tag_names=applied_tag_names,
            posted=True,
        )

    def build_thread_title(self, announcement: Announcement) -> str:
        title = normalize_text(announcement.title)
        fallback_title = normalize_text(announcement.category) or "未分類"
        return self._truncate(title or fallback_title, limit=100)

    def build_initial_message(self, announcement: Announcement) -> str:
        if announcement.source_url:
            return f"原始公告：{announcement.source_url}"
        return "原始公告連結：無公開連結"

    def build_embed(self, announcement: Announcement) -> discord.Embed:
        description = self._build_description(announcement)
        embed = discord.Embed(
            title=self._truncate(normalize_text(announcement.title), limit=256),
            url=announcement.source_url or None,
            description=self._truncate(description, limit=4096),
            color=discord.Color.red(),
        )
        timestamp = self._extract_timestamp(announcement)
        if timestamp is not None:
            embed.timestamp = timestamp
        else:
            embed.add_field(name="日期", value=normalize_text(announcement.date) or "未知", inline=True)
        embed.add_field(name="類別", value=normalize_text(announcement.category) or "未分類", inline=True)
        embed.add_field(name="單位", value=normalize_text(announcement.unit) or "未知", inline=True)

        if announcement.attachments:
            embed.add_field(
                name="附件",
                value=self._format_field_value(
                    self._format_links(announcement.attachments, limit=5)
                ),
                inline=False,
            )
        if announcement.external_links:
            embed.add_field(
                name="相關連結",
                value=self._format_field_value(
                    self._format_links(announcement.external_links, limit=5)
                ),
                inline=False,
            )
        if announcement.important_dates:
            embed.add_field(
                name="可能重要日期",
                value=self._format_field_value("\n".join(announcement.important_dates[:5])),
                inline=False,
            )

        image_url = self._extract_first_image_url(announcement)
        if image_url:
            embed.set_image(url=image_url)

        embed.set_footer(text="來源：國立中興大學附屬高級中學公告")
        return embed

    def _format_links(self, links: Iterable[object], *, limit: int) -> str:
        lines: list[str] = []
        for index, link in enumerate(links):
            if index >= limit:
                break
            name = normalize_text(getattr(link, "name", "") or getattr(link, "label", "連結"))
            url = normalize_text(getattr(link, "url", ""))
            if not url:
                continue
            display_name = self._normalize_link_display_name(name=name, url=url)
            lines.append(f"- [{display_name}]({url})")
        return "\n".join(lines) if lines else "無"

    def _truncate(self, value: str, *, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 1].rstrip() + "…"

    def _format_field_value(self, value: str) -> str:
        normalized = self._normalize_multiline_text(value)
        return self._truncate(normalized or "無", limit=self.FIELD_VALUE_LIMIT)

    def _build_description(self, announcement: Announcement) -> str:
        if announcement.content_html:
            description = self._html_to_multiline_text(announcement.content_html)
            if description:
                return description
        if announcement.content_text:
            return self._normalize_multiline_text(announcement.content_text)
        return self._normalize_multiline_text(announcement.excerpt or "暫無摘要")

    def _html_to_multiline_text(self, content_html: str) -> str:
        html = (content_html or "").strip()
        if not html:
            return ""
        if self._looks_like_url(html):
            return html

        soup = BeautifulSoup(html, "lxml")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for li in soup.find_all("li"):
            li.insert(0, "- ")
            li.append("\n")
        for block in soup.find_all(["p", "div", "section", "article", "tr", "table"]):
            block.append("\n\n")
        return self._normalize_multiline_text(soup.get_text())

    def _normalize_multiline_text(self, value: str) -> str:
        raw_lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        normalized_lines: list[str] = []
        previous_blank = False
        for raw_line in raw_lines:
            line = normalize_text(raw_line)
            if line:
                normalized_lines.append(line)
                previous_blank = False
            elif normalized_lines and not previous_blank:
                normalized_lines.append("")
                previous_blank = True
        return "\n".join(normalized_lines).strip()

    def _extract_timestamp(self, announcement: Announcement) -> datetime | None:
        detail_item = announcement.raw_payload.get("detail_item", {})
        candidates = [
            normalize_text(detail_item.get("time") if isinstance(detail_item, dict) else ""),
            normalize_text(announcement.date),
        ]
        formats = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d")
        for candidate in candidates:
            if not candidate:
                continue
            for fmt in formats:
                try:
                    parsed = datetime.strptime(candidate, fmt)
                    return parsed.replace(tzinfo=UTC)
                except ValueError:
                    continue
        return None

    def _extract_first_image_url(self, announcement: Announcement) -> str | None:
        html = (announcement.content_html or "").strip()
        if not html or self._looks_like_url(html):
            return None
        soup = BeautifulSoup(html, "lxml")
        base_url = normalize_text(announcement.raw_payload.get("root_path") if isinstance(announcement.raw_payload, dict) else "") or announcement.source_url or ""
        for image in soup.select("img[src]"):
            src = normalize_text(image.get("src"))
            if not src or src.startswith("data:"):
                continue
            return urljoin(base_url, src)
        return None

    def _normalize_link_display_name(self, *, name: str, url: str) -> str:
        if not name or self._looks_like_url(name) or name == url:
            return self._extract_domain(url)
        return name

    def _extract_domain(self, url: str) -> str:
        parsed = urlparse(url)
        hostname = parsed.netloc.lower() or url
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname

    def _looks_like_url(self, value: str) -> bool:
        parsed = urlparse(value)
        return bool(parsed.scheme and parsed.netloc and "<" not in value and ">" not in value)

    def _merge_tags(
        self,
        first: Iterable[discord.ForumTag],
        second: Iterable[discord.ForumTag],
    ) -> list[discord.ForumTag]:
        merged: list[discord.ForumTag] = []
        seen_ids: set[int] = set()
        for tag in [*first, *second]:
            if tag.id in seen_ids:
                continue
            seen_ids.add(tag.id)
            merged.append(tag)
        return merged

    def _forum_requires_tag(self, forum: discord.ForumChannel) -> bool:
        flags = getattr(forum, "flags", None)
        return bool(getattr(flags, "require_tag", False))