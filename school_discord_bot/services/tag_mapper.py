from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import discord

from school_discord_bot.db.database import Database
from school_discord_bot.models.announcement import normalize_text


DEFAULT_CATEGORY_TAGS: tuple[str, ...] = (
    "一般公告",
    "競賽資訊",
    "課程活動",
    "大學升學",
    "新生入學",
    "獎助學金",
    "榮譽事蹟",
    "研習活動",
    "自主學習",
    "學習歷程",
)
DEFAULT_UNIT_TAGS: tuple[str, ...] = (
    "訓育組",
    "設備組",
    "教學組",
    "註冊組",
    "實研組",
    "圖書館",
    "衛生組",
    "輔導室",
    "試務組",
)
FALLBACK_TAG_CANDIDATES: tuple[str, ...] = ("一般公告", "未分類")


@dataclass(slots=True)
class TagSyncResult:
    created: list[str]
    mapped: list[str]
    missing_permissions: bool


def find_tag_by_reference(available_tags: Sequence[Any], reference: str | int | None) -> Any | None:
    if reference is None:
        return None

    tag_id: int | None = None
    tag_name: str | None = None
    if isinstance(reference, int):
        tag_id = reference
    else:
        text = normalize_text(str(reference))
        if text.isdigit():
            tag_id = int(text)
        else:
            tag_name = text.casefold()

    if tag_id is not None:
        for tag in available_tags:
            if getattr(tag, "id", None) == tag_id:
                return tag

    if tag_name is not None:
        for tag in available_tags:
            if normalize_text(getattr(tag, "name", "")).casefold() == tag_name:
                return tag

    return None


class TagMapper:
    """Resolve school categories to Discord forum tags and keep mappings in SQLite."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def sync_known_tags(self, forum: discord.ForumChannel) -> TagSyncResult:
        created: list[str] = []
        mapped: list[str] = []
        bot_member = forum.guild.me
        if bot_member is None:
            self_id = getattr(getattr(forum, "_state", None), "self_id", None)
            if self_id is not None:
                try:
                    bot_member = await forum.guild.fetch_member(self_id)
                except discord.HTTPException:
                    bot_member = None

        missing_permissions = True
        if bot_member is not None:
            missing_permissions = not forum.permissions_for(bot_member).manage_channels
        available_tags = list(forum.available_tags)

        for tag_name in [*DEFAULT_CATEGORY_TAGS, *FALLBACK_TAG_CANDIDATES[1:], *DEFAULT_UNIT_TAGS]:
            tag = find_tag_by_reference(available_tags, tag_name)
            if tag is None and not missing_permissions:
                tag = await forum.create_tag(name=tag_name, reason="同步學校公告標籤")
                available_tags.append(tag)
                created.append(tag_name)
            if tag is not None:
                if tag_name in DEFAULT_CATEGORY_TAGS or tag_name in FALLBACK_TAG_CANDIDATES:
                    await self.database.upsert_tag_mapping(
                        category=tag_name,
                        forum_tag_id=tag.id,
                        forum_tag_name=tag.name,
                    )
                mapped.append(tag_name)

        return TagSyncResult(created=created, mapped=mapped, missing_permissions=missing_permissions)

    async def set_manual_mapping(
        self,
        *,
        category: str,
        forum: discord.ForumChannel,
        tag_reference: str,
    ) -> discord.ForumTag:
        tag = find_tag_by_reference(forum.available_tags, tag_reference)
        if tag is None:
            raise LookupError(f"tag not found: {tag_reference}")

        await self.database.upsert_tag_mapping(
            category=normalize_text(category),
            forum_tag_id=tag.id,
            forum_tag_name=tag.name,
        )
        return tag

    async def resolve_tags_for_category(
        self,
        *,
        forum: discord.ForumChannel,
        category: str,
    ) -> list[discord.ForumTag]:
        normalized_category = normalize_text(category)
        mapping = await self.database.get_tag_mapping(normalized_category)
        if mapping:
            tag = find_tag_by_reference(
                forum.available_tags,
                mapping.get("forum_tag_id") or mapping.get("forum_tag_name"),
            )
            if tag is not None:
                return [tag]

        direct_tag = find_tag_by_reference(forum.available_tags, normalized_category)
        if direct_tag is not None:
            return [direct_tag]

        fallback = await self.resolve_fallback_tags(forum)
        return fallback

    def resolve_existing_unit_tag(
        self,
        *,
        forum: discord.ForumChannel,
        unit: str,
    ) -> discord.ForumTag | None:
        normalized_unit = normalize_text(unit)
        if not normalized_unit:
            return None
        if normalized_unit not in DEFAULT_UNIT_TAGS:
            return None
        tag = find_tag_by_reference(forum.available_tags, normalized_unit)
        if tag is None:
            return None
        return tag

    async def resolve_fallback_tags(self, forum: discord.ForumChannel) -> list[discord.ForumTag]:
        for candidate in FALLBACK_TAG_CANDIDATES:
            mapping = await self.database.get_tag_mapping(candidate)
            if mapping:
                tag = find_tag_by_reference(
                    forum.available_tags,
                    mapping.get("forum_tag_id") or mapping.get("forum_tag_name"),
                )
                if tag is not None:
                    return [tag]

            tag = find_tag_by_reference(forum.available_tags, candidate)
            if tag is not None:
                return [tag]

        return []