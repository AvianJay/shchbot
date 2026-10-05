from __future__ import annotations

import asyncio
import ssl

import aiohttp
import pytest

from school_discord_bot.models.announcement import Announcement
from school_discord_bot.services.announcement_parser import ParsedListPage
from school_discord_bot.services.school_news_client import SchoolNewsClient


def build_client(*, allow_insecure_ssl_fallback: bool = True) -> SchoolNewsClient:
    return SchoolNewsClient(
        session=None,  # type: ignore[arg-type]
        widget_url="https://www.dali.tc.edu.tw/ischool/widget/site_news/main2.php?allbtn=0&maximize=1&uid=test",
        timeout_seconds=20,
        user_agent="test-agent",
        allow_insecure_ssl_fallback=allow_insecure_ssl_fallback,
    )


def test_should_use_insecure_ssl_fallback_for_trusted_host() -> None:
    client = build_client()
    ssl_error = ssl.SSLCertVerificationError("certificate verify failed")
    exc = aiohttp.ClientSSLError(None, ssl_error)

    assert client._should_use_insecure_ssl_fallback("https://www.dali.tc.edu.tw/home", exc)


def test_should_not_use_insecure_ssl_fallback_for_other_host() -> None:
    client = build_client()
    ssl_error = ssl.SSLCertVerificationError("certificate verify failed")
    exc = aiohttp.ClientSSLError(None, ssl_error)

    assert not client._should_use_insecure_ssl_fallback("https://example.com/home", exc)


def test_should_not_use_insecure_ssl_fallback_when_disabled() -> None:
    client = build_client(allow_insecure_ssl_fallback=False)
    ssl_error = ssl.SSLCertVerificationError("certificate verify failed")
    exc = aiohttp.ClientSSLError(None, ssl_error)

    assert not client._should_use_insecure_ssl_fallback("https://www.dali.tc.edu.tw/home", exc)


def listed(source_id: str, *, pinned: bool = False) -> Announcement:
    return Announcement(
        source_id=source_id,
        source_hash=f"hash-{source_id}",
        source_url=f"https://example.com/news/{source_id}",
        title=f"公告 {source_id}",
        date="2026/10/05",
        category="一般公告",
        unit="註冊組",
        pinned=pinned,
    )


def serve_pages(
    monkeypatch: pytest.MonkeyPatch,
    client: SchoolNewsClient,
    pages: list[list[Announcement]],
) -> list[int]:
    requested: list[int] = []

    async def fake_fetch_page(*, page_num: int = 0, max_rows: int = 10, **_: object) -> ParsedListPage:
        requested.append(page_num)
        return ParsedListPage(announcements=pages[page_num], total_pages=len(pages))

    monkeypatch.setattr(client, "fetch_page", fake_fetch_page)
    return requested


def test_pinned_announcements_do_not_use_up_the_fetch_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: 24 pinned posts filled every 5-row poll, so unpinned posts were never seen."""
    client = build_client()
    requested = serve_pages(
        monkeypatch,
        client,
        [
            [listed("1", pinned=True), listed("2", pinned=True), listed("3", pinned=True)],
            [listed("20410"), listed("20409"), listed("20407")],
        ],
    )

    announcements = asyncio.run(client.fetch_latest_announcements(limit=2, include_details=False))

    assert [announcement.source_id for announcement in announcements] == ["1", "2", "3", "20410", "20409"]
    assert requested == [0, 1]


def test_fetch_stops_at_the_last_page(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build_client()
    requested = serve_pages(monkeypatch, client, [[listed("1", pinned=True), listed("20410")]])

    announcements = asyncio.run(client.fetch_latest_announcements(limit=30, include_details=False))

    assert [announcement.source_id for announcement in announcements] == ["1", "20410"]
    assert requested == [0]