from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import json
import re
from typing import Any


class PostStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    REJECTED = "rejected"
    REMOVED = "removed"


# Emoji live in the category name itself. A unicode emoji in a select label
# always renders, whereas a separate select-option emoji must be one Discord
# accepts — a bad or since-deleted one makes the whole submission modal fail.
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "😡 我要靠北",
    "💌 我要告白",
    "👍 我要讚美",
    "❓ 我要發問",
    "🔍 我要協尋",
)

SUBMIT_COOLDOWN_SECONDS = 300
MAX_IMAGES = 4  # Discord merges at most four same-url embeds into one gallery.
MAX_CONTENT_LENGTH = 2000
MAX_REASON_LENGTH = 500
MAX_CATEGORIES = 25  # A string select holds at most 25 options.
MAX_CATEGORY_NAME_LENGTH = 30
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_THREAD_NAME_LENGTH = 100

_WHITESPACE_RE = re.compile(r"\s+")


def setting_key(guild_id: int) -> str:
    return f"anon_board:{guild_id}"


def collapse_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def thread_name(number: int, category_name: str, content: str) -> str:
    """Name a post's comment thread, e.g. ``#12 😡 我要靠北｜今天午餐…``."""
    head = collapse_whitespace(f"#{number} {category_name}")
    preview = collapse_whitespace(content)
    name = f"{head}｜{preview}" if preview else head
    if len(name) > MAX_THREAD_NAME_LENGTH:
        name = name[: MAX_THREAD_NAME_LENGTH - 1] + "…"
    return name


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


@dataclass(slots=True, frozen=True)
class AnonymousCategory:
    id: int
    name: str

    @classmethod
    def from_database_row(cls, row: Any) -> "AnonymousCategory":
        return cls(id=int(row["id"]), name=str(row["name"]))


@dataclass(slots=True)
class AnonymousPost:
    id: int
    author_id: int
    author_name: str
    category_name: str
    content: str
    image_count: int
    status: PostStatus
    created_at: float
    public_number: int | None = None
    public_channel_id: int | None = None
    public_message_id: int | None = None
    review_channel_id: int | None = None
    review_message_id: int | None = None
    moderator_id: int | None = None
    moderator_reason: str | None = None
    moderated_at: float | None = None

    @classmethod
    def from_database_row(cls, row: Any) -> "AnonymousPost":
        return cls(
            id=int(row["id"]),
            author_id=int(row["author_id"]),
            author_name=str(row["author_name"] or ""),
            category_name=str(row["category_name"]),
            content=str(row["content"]),
            image_count=int(row["image_count"]),
            status=PostStatus(row["status"]),
            created_at=float(row["created_at"]),
            public_number=_optional_int(row["public_number"]),
            public_channel_id=_optional_int(row["public_channel_id"]),
            public_message_id=_optional_int(row["public_message_id"]),
            review_channel_id=_optional_int(row["review_channel_id"]),
            review_message_id=_optional_int(row["review_message_id"]),
            moderator_id=_optional_int(row["moderator_id"]),
            moderator_reason=row["moderator_reason"],
            moderated_at=float(row["moderated_at"]) if row["moderated_at"] is not None else None,
        )


@dataclass(slots=True, frozen=True)
class AnonymousBoardConfig:
    public_channel_id: int
    review_channel_id: int
    require_review: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "AnonymousBoardConfig":
        payload = json.loads(raw)
        return cls(
            public_channel_id=int(payload["public_channel_id"]),
            review_channel_id=int(payload["review_channel_id"]),
            require_review=bool(payload["require_review"]),
        )
