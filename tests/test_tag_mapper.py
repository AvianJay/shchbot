from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from school_discord_bot.services.tag_mapper import TagMapper, find_tag_by_reference


@dataclass
class FakeTag:
    id: int
    name: str


@pytest.mark.parametrize(
    ("reference", "expected_id"),
    [
        ("一般公告", 1),
        ("1", 1),
        (1, 1),
        ("課程活動", 2),
    ],
)
def test_find_tag_by_reference(reference: str | int, expected_id: int) -> None:
    tags = [FakeTag(id=1, name="一般公告"), FakeTag(id=2, name="課程活動")]
    tag = find_tag_by_reference(tags, reference)
    assert tag is not None
    assert tag.id == expected_id


def test_find_tag_by_reference_returns_none_when_missing() -> None:
    tags = [FakeTag(id=1, name="一般公告")]
    assert find_tag_by_reference(tags, "不存在") is None


def test_resolve_existing_unit_tag_only_allows_whitelist() -> None:
    mapper = TagMapper(database=SimpleNamespace())
    forum = SimpleNamespace(
        available_tags=[
            FakeTag(id=1, name="訓育組"),
            FakeTag(id=2, name="生輔組"),
        ]
    )

    allowed = mapper.resolve_existing_unit_tag(forum=forum, unit="訓育組")
    blocked = mapper.resolve_existing_unit_tag(forum=forum, unit="生輔組")

    assert allowed is not None
    assert allowed.name == "訓育組"
    assert blocked is None


def test_sync_known_tags_creates_whitelisted_unit_tags_without_mapping_them() -> None:
    upserts: list[tuple[str, int, str]] = []

    class FakeDatabase:
        async def upsert_tag_mapping(self, *, category: str, forum_tag_id: int, forum_tag_name: str) -> None:
            upserts.append((category, forum_tag_id, forum_tag_name))

    class FakePermissions:
        manage_channels = True

    class FakeForum:
        def __init__(self) -> None:
            self.available_tags = [FakeTag(id=1, name="一般公告")]
            self.guild = SimpleNamespace(me=object())
            self.created_names: list[str] = []
            self._next_id = 2

        def permissions_for(self, member: object) -> FakePermissions:
            return FakePermissions()

        async def create_tag(self, *, name: str, reason: str) -> FakeTag:
            tag = FakeTag(id=self._next_id, name=name)
            self._next_id += 1
            self.available_tags.append(tag)
            self.created_names.append(name)
            return tag

    async def run_test() -> tuple[object, list[tuple[str, int, str]]]:
        mapper = TagMapper(database=FakeDatabase())
        forum = FakeForum()
        result = await mapper.sync_known_tags(forum)
        return result, upserts

    result, captured_upserts = asyncio.run(run_test())

    assert "訓育組" in result.created
    assert "設備組" in result.created
    assert "衛生組" in result.created
    assert any(category == "一般公告" for category, _, _ in captured_upserts)
    assert all(category != "訓育組" for category, _, _ in captured_upserts)