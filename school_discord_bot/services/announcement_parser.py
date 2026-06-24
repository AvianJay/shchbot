from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from school_discord_bot.models.announcement import (
    Announcement,
    AttachmentLink,
    ExternalLink,
    build_source_hash,
    extract_inner_tag_text,
    normalize_text,
    strip_category_brackets,
)


_ROOT_PATH_PATTERN = re.compile(r'var\s+g_root_path\s*=\s*"([^"]+)"')
_UID_PATTERN = re.compile(r'var\s+g_unique_id\s*=\s*"([^"]+)"')
_ATTACHED_FILE_PATTERN = re.compile(r"g_attached_file_json_data\s*=\s*'([^']*)'")
_IMPORTANT_DATE_PATTERNS = (
    re.compile(
        r"(?:截止|報名|期限|甄選|考試)[^\n。]{0,24}?((?:\d{2,4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2})(?:\([^)]+\))?)"
    ),
    re.compile(
        r"((?:\d{2,4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2})(?:\([^)]+\))?)[^\n。]{0,24}?(?:截止|報名|期限|甄選|考試)"
    ),
)


class DetailPermissionError(RuntimeError):
    """Raised when the public detail endpoint is not accessible."""


@dataclass(slots=True)
class WidgetConfig:
    uid: str
    root_path: str
    widget_url: str
    widget_base_url: str
    list_endpoint: str
    detail_endpoint: str
    detail_page_endpoint: str
    public_view_base_url: str


@dataclass(slots=True)
class ParsedListPage:
    announcements: list[Announcement]
    total_pages: int


def parse_widget_config(html: str, widget_url: str) -> WidgetConfig:
    parsed_widget = urlparse(widget_url)
    query_uid = parse_qs(parsed_widget.query).get("uid", [""])[0]
    root_path_match = _ROOT_PATH_PATTERN.search(html)
    uid_match = _UID_PATTERN.search(html)

    root_path = root_path_match.group(1) if root_path_match else f"{parsed_widget.scheme}://{parsed_widget.netloc}/ischool/"
    uid = uid_match.group(1) if uid_match else query_uid
    widget_base_url = widget_url.rsplit("/", 1)[0] + "/"

    return WidgetConfig(
        uid=uid,
        root_path=root_path,
        widget_url=widget_url,
        widget_base_url=widget_base_url,
        list_endpoint=urljoin(widget_base_url, "news_query_json.php"),
        detail_endpoint=urljoin(widget_base_url, "news_query_json_content.php"),
        detail_page_endpoint=urljoin(widget_base_url, "news_pop_content.php"),
        public_view_base_url=urljoin(root_path, "public/news_view/show.php"),
    )


def build_public_news_url(public_view_base_url: str, news_id: str) -> str:
    return f"{public_view_base_url}?nid={news_id}"


def parse_announcements_from_table_html(html: str, config: WidgetConfig) -> list[Announcement]:
    soup = BeautifulSoup(html, "lxml")
    announcements: list[Announcement] = []

    for row in soup.select("#ntb tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue

        anchor = row.select_one("a#content_href, a[href]")
        title = normalize_text(anchor.get_text(" ", strip=True) if anchor else cells[4].get_text(" ", strip=True))
        category = strip_category_brackets(cells[2].get_text(" ", strip=True))
        unit = normalize_text(cells[3].get_text(" ", strip=True))
        news_id = normalize_text((anchor or row).get("nid"))
        source_url = build_public_news_url(config.public_view_base_url, news_id) if news_id else None
        announcement = Announcement(
            source_id=news_id,
            source_hash=build_source_hash(
                source_url,
                date=cells[1].get_text(" ", strip=True),
                category=category,
                unit=unit,
                title=title,
            ),
            source_url=source_url,
            title=title,
            date=normalize_text(cells[1].get_text(" ", strip=True)),
            category=category,
            unit=unit,
            raw_payload={"html_row": str(row)},
            view_count=_parse_int(cells[5].get_text(" ", strip=True)),
            inner_tag_text=extract_inner_tag_text(title),
        )
        announcements.append(announcement)

    return announcements


def parse_list_json(payload: list[dict[str, Any]], config: WidgetConfig) -> ParsedListPage:
    if not payload:
        return ParsedListPage(announcements=[], total_pages=0)

    meta = payload[0]
    announcements: list[Announcement] = []

    for record in payload[1:]:
        news_id = normalize_text(str(record.get("newsId", "")))
        title = normalize_text(record.get("title_hint") or record.get("title"))
        category = strip_category_brackets(record.get("attr_name"))
        unit = normalize_text(record.get("unit_name"))
        source_url = build_public_news_url(config.public_view_base_url, news_id) if news_id else None
        announcement = Announcement(
            source_id=news_id,
            source_hash=build_source_hash(
                source_url,
                date=record.get("time", ""),
                category=category,
                unit=unit,
                title=title,
            ),
            source_url=source_url,
            title=title,
            date=normalize_text(record.get("time")),
            category=category,
            unit=unit,
            raw_payload={"list_item": record},
            view_count=_parse_int(record.get("clicks")),
            inner_tag_text=extract_inner_tag_text(title),
        )

        if normalize_text(record.get("content_type")) == "url" and normalize_text(record.get("content")):
            announcement.external_links.append(
                ExternalLink(label="外部連結", url=normalize_text(record.get("content")))
            )

        announcements.append(announcement)

    return ParsedListPage(
        announcements=announcements,
        total_pages=int(meta.get("totalPages", 0) or 0),
    )


def parse_detail_json(
    payload: list[dict[str, Any]],
    announcement: Announcement,
    root_path: str,
) -> Announcement:
    if not payload:
        return announcement

    record = payload[0]
    rcode = int(record.get("rcode", 0) or 0)
    if rcode in {401, 402, 403}:
        raise DetailPermissionError(f"detail endpoint returned permission error: {rcode}")
    if rcode != 200:
        raise ValueError(f"unexpected detail response code: {rcode}")

    content_html = _decode_content_html(record.get("content", ""))
    content_text = extract_text_from_html(content_html)
    excerpt = build_excerpt(content_text)
    attachments = parse_attached_files(
        record.get("attachedfile", "[]"),
        root_path=root_path,
        news_id=normalize_text(str(record.get("newsId", announcement.source_id))),
    )
    external_links = _merge_external_links(
        announcement.external_links,
        extract_external_links(content_html, base_url=announcement.source_url or root_path),
    )
    important_dates = extract_important_dates(content_text)

    merged_payload = dict(announcement.raw_payload)
    merged_payload["detail_item"] = record
    merged_payload["root_path"] = root_path

    return replace(
        announcement,
        raw_payload=merged_payload,
        excerpt=excerpt,
        content_html=content_html,
        content_text=content_text,
        attachments=attachments,
        external_links=external_links,
        important_dates=important_dates,
        unit=normalize_text(record.get("unit")) or announcement.unit,
    )


def parse_detail_page_html(html: str, announcement: Announcement, root_path: str) -> Announcement:
    soup = BeautifulSoup(html, "lxml")
    content_node = soup.select_one("#content, .contentArea")
    content_html = content_node.decode_contents() if content_node else ""
    content_text = extract_text_from_html(content_html)
    excerpt = build_excerpt(content_text)

    attachments = [
        AttachmentLink(name=normalize_text(anchor.get_text(" ", strip=True)), url=urljoin(root_path, anchor.get("href", "")))
        for anchor in soup.select("#trAttachment a[href]")
        if normalize_text(anchor.get("href"))
    ]
    if not attachments:
        raw_attached_data = _ATTACHED_FILE_PATTERN.search(html)
        if raw_attached_data:
            attachments = parse_attached_files(
                raw_attached_data.group(1),
                root_path=root_path,
                news_id=announcement.source_id,
            )

    external_links = _merge_external_links(
        announcement.external_links,
        extract_external_links(content_html, base_url=announcement.source_url or root_path),
    )
    important_dates = extract_important_dates(content_text)
    detail_time = normalize_text((soup.select_one("#info_time") or {}).get_text(" ", strip=True) if soup.select_one("#info_time") else "")
    detail_unit = normalize_text((soup.select_one("#info_unit") or {}).get_text(" ", strip=True) if soup.select_one("#info_unit") else "")

    merged_payload = dict(announcement.raw_payload)
    merged_payload["detail_html"] = {"has_attachment": bool(attachments)}
    merged_payload["root_path"] = root_path

    return replace(
        announcement,
        raw_payload=merged_payload,
        excerpt=excerpt,
        content_html=content_html,
        content_text=content_text,
        attachments=attachments,
        external_links=external_links,
        important_dates=important_dates,
        date=announcement.date or detail_time,
        unit=detail_unit or announcement.unit,
    )


def extract_text_from_html(content_html: str) -> str:
    soup = BeautifulSoup(content_html or "", "lxml")
    return normalize_text(soup.get_text(" ", strip=True))


def build_excerpt(content_text: str, limit: int = 220) -> str:
    text = normalize_text(content_text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def extract_external_links(content_html: str, base_url: str) -> list[ExternalLink]:
    soup = BeautifulSoup(content_html or "", "lxml")
    seen: set[str] = set()
    links: list[ExternalLink] = []

    for anchor in soup.select("a[href]"):
        href = normalize_text(anchor.get("href"))
        if not href or href.startswith("javascript:") or href.startswith("mailto:"):
            continue
        absolute_url = urljoin(base_url, href)
        if absolute_url in seen:
            continue
        seen.add(absolute_url)
        links.append(
            ExternalLink(
                label=normalize_text(anchor.get_text(" ", strip=True)) or absolute_url,
                url=absolute_url,
            )
        )

    return links


def extract_important_dates(content_text: str) -> list[str]:
    found: list[str] = []
    for pattern in _IMPORTANT_DATE_PATTERNS:
        for match in pattern.finditer(content_text):
            value = normalize_text(match.group(1))
            if value and value not in found:
                found.append(value)
    return found


def parse_attached_files(attached_file_data: str | list[Any], root_path: str, news_id: str) -> list[AttachmentLink]:
    if isinstance(attached_file_data, str):
        raw_data = attached_file_data.strip() or "[]"
        try:
            parsed_data = json.loads(raw_data)
        except json.JSONDecodeError:
            parsed_data = json.loads(_decode_js_escaped_text(raw_data))
    else:
        parsed_data = attached_file_data

    attachments: list[AttachmentLink] = []
    for item in parsed_data:
        if not item or len(item) < 3:
            continue
        file_name = _decode_js_escaped_text(str(item[2]))
        encoded_name = quote(file_name, safe="").replace("%20", "+")
        url = urljoin(root_path, f"news/attached/{news_id}/{encoded_name}")
        attachments.append(AttachmentLink(name=file_name, url=url))
    return attachments


def _decode_content_html(value: str | None) -> str:
    return unquote(value or "")


def _decode_js_escaped_text(value: str) -> str:
    def replace_unicode(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    partially_decoded = re.sub(r"%u([0-9A-Fa-f]{4})", replace_unicode, value)
    return unquote(partially_decoded)


def _merge_external_links(
    current_links: list[ExternalLink],
    extra_links: list[ExternalLink],
) -> list[ExternalLink]:
    merged: list[ExternalLink] = []
    seen: set[str] = set()
    for link in [*current_links, *extra_links]:
        if link.url in seen:
            continue
        seen.add(link.url)
        merged.append(link)
    return merged


def _parse_int(value: Any) -> int | None:
    text = normalize_text(str(value))
    return int(text) if text.isdigit() else None