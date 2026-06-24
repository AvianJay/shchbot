from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_FULL_WIDTH_SPACE = "\u3000"
_LEADING_TAG_PATTERN = re.compile(r"^((?:[【\[].+?[】\]])+)")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.replace(_FULL_WIDTH_SPACE, " ")
    normalized = _WHITESPACE_PATTERN.sub(" ", normalized)
    return normalized.strip()


def strip_category_brackets(value: str | None) -> str:
    text = normalize_text(value)
    if (text.startswith("【") and text.endswith("】")) or (
        text.startswith("[") and text.endswith("]")
    ):
        return text[1:-1].strip()
    return text


def canonicalize_url(url: str | None) -> str | None:
    if not url:
        return None

    text = normalize_text(url)
    parsed = urlsplit(text)
    filtered_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "fbclid" and not key.lower().startswith("utm_")
    ]
    canonical_query = urlencode(sorted(filtered_items), doseq=True)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            canonical_query,
            "",
        )
    )


def build_source_hash(
    source_url: str | None,
    *,
    date: str,
    category: str,
    unit: str,
    title: str,
) -> str:
    canonical_url = canonicalize_url(source_url)
    seed = canonical_url or "|".join(
        [
            normalize_text(date),
            normalize_text(category),
            normalize_text(unit),
            normalize_text(title),
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def extract_inner_tag_text(title: str | None) -> str | None:
    text = normalize_text(title)
    if not text:
        return None
    match = _LEADING_TAG_PATTERN.match(text)
    if not match:
        return None
    return match.group(1)


@dataclass(slots=True)
class AttachmentLink:
    name: str
    url: str


@dataclass(slots=True)
class ExternalLink:
    label: str
    url: str


@dataclass(slots=True)
class Announcement:
    source_id: str
    source_hash: str
    source_url: str | None
    title: str
    date: str
    category: str
    unit: str
    excerpt: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    view_count: int | None = None
    inner_tag_text: str | None = None
    content_html: str | None = None
    content_text: str | None = None
    attachments: list[AttachmentLink] = field(default_factory=list)
    external_links: list[ExternalLink] = field(default_factory=list)
    important_dates: list[str] = field(default_factory=list)
    posted_at: str | None = None
    discord_thread_id: int | None = None

    def to_raw_json(self) -> str:
        payload = {
            "source_id": self.source_id,
            "raw_payload": self.raw_payload,
            "view_count": self.view_count,
            "inner_tag_text": self.inner_tag_text,
            "content_html": self.content_html,
            "content_text": self.content_text,
            "attachments": [asdict(attachment) for attachment in self.attachments],
            "external_links": [asdict(link) for link in self.external_links],
            "important_dates": self.important_dates,
        }
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_database_row(cls, row: Any) -> "Announcement":
        payload = json.loads(row["raw_json"]) if row["raw_json"] else {}
        attachments = [AttachmentLink(**item) for item in payload.get("attachments", [])]
        external_links = [ExternalLink(**item) for item in payload.get("external_links", [])]

        return cls(
            source_id=str(row["source_id"] or payload.get("source_id") or ""),
            source_hash=row["source_hash"],
            source_url=row["source_url"],
            title=row["title"],
            date=row["date"],
            category=row["category"],
            unit=row["unit"],
            excerpt=row["excerpt"],
            raw_payload=payload.get("raw_payload", {}),
            view_count=row["view_count"],
            inner_tag_text=row["inner_tag_text"],
            content_html=payload.get("content_html"),
            content_text=payload.get("content_text"),
            attachments=attachments,
            external_links=external_links,
            important_dates=payload.get("important_dates", []),
            posted_at=row["posted_at"],
            discord_thread_id=row["discord_thread_id"],
        )