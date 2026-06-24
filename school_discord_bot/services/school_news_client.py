from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import ssl
from typing import Any
from urllib.parse import urlparse

import aiohttp

from school_discord_bot.services.announcement_parser import (
    DetailPermissionError,
    ParsedListPage,
    WidgetConfig,
    parse_announcements_from_table_html,
    parse_detail_json,
    parse_detail_page_html,
    parse_list_json,
    parse_widget_config,
)


@dataclass(slots=True)
class ScraperProbeResult:
    latest_title: str | None
    total_pages: int
    widget_uid: str | None


class SchoolNewsClient:
    """Fetch and parse the school's public announcement widget and detail pages."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        widget_url: str,
        timeout_seconds: int,
        user_agent: str,
        allow_insecure_ssl_fallback: bool,
        logger: logging.Logger | None = None,
    ) -> None:
        self.session = session
        self.widget_url = widget_url
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.allow_insecure_ssl_fallback = allow_insecure_ssl_fallback
        self.logger = logger or logging.getLogger(__name__)
        self._widget_config: WidgetConfig | None = None
        self._widget_html_cache: str | None = None
        self._trusted_host = (urlparse(widget_url).hostname or "").lower()
        self._insecure_ssl_context = self._build_insecure_ssl_context()

    async def get_widget_config(self, *, force_refresh: bool = False) -> WidgetConfig:
        if self._widget_config is not None and not force_refresh:
            return self._widget_config

        html = await self._request_text(self.widget_url)
        self._widget_html_cache = html
        self._widget_config = parse_widget_config(html, self.widget_url)
        return self._widget_config

    async def fetch_page(
        self,
        *,
        page_num: int = 0,
        max_rows: int = 10,
        keyword: str = "",
        flock: str = "",
    ) -> ParsedListPage:
        config = await self.get_widget_config()
        payload = await self._request_json(
            config.list_endpoint,
            method="POST",
            data={
                "field": "time",
                "order": "DESC",
                "pageNum": str(page_num),
                "maxRows": str(max_rows),
                "keyword": keyword,
                "uid": config.uid,
                "tf": "1",
                "auth_type": "user",
                "flock": flock,
            },
        )

        if isinstance(payload, list):
            parsed = parse_list_json(payload, config)
            if parsed.announcements:
                return parsed

        if page_num == 0 and self._widget_html_cache:
            fallback_announcements = parse_announcements_from_table_html(
                self._widget_html_cache,
                config,
            )
            return ParsedListPage(announcements=fallback_announcements, total_pages=1)

        return ParsedListPage(announcements=[], total_pages=0)

    async def fetch_latest_announcements(
        self,
        *,
        limit: int,
        include_details: bool = True,
    ) -> list[Any]:
        collected = []
        seen_hashes: set[str] = set()
        page_num = 0
        page_size = max(1, min(limit, 30))

        while len(collected) < limit:
            page = await self.fetch_page(page_num=page_num, max_rows=page_size)
            if not page.announcements:
                break

            for announcement in page.announcements:
                if announcement.source_hash in seen_hashes:
                    continue
                seen_hashes.add(announcement.source_hash)
                collected.append(announcement)
                if len(collected) >= limit:
                    break

            page_num += 1
            if page.total_pages <= page_num:
                break

        if not include_details:
            return collected[:limit]

        detailed_announcements = []
        for announcement in collected[:limit]:
            detailed_announcements.append(await self.enrich_announcement(announcement))
        return detailed_announcements

    async def enrich_announcement(self, announcement: Any) -> Any:
        config = await self.get_widget_config()
        if not announcement.source_id:
            return announcement

        try:
            payload = await self._request_json(
                config.detail_endpoint,
                params={"nid": announcement.source_id, "dir": "0", "uid": config.uid},
            )
            if isinstance(payload, list):
                return parse_detail_json(payload, announcement, config.root_path)
        except DetailPermissionError:
            self.logger.info(
                "Skipping protected detail endpoint for news id %s",
                announcement.source_id,
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to fetch detail JSON for announcement %s: %s",
                announcement.source_id,
                exc,
            )

        try:
            detail_html = await self._request_text(
                config.detail_page_endpoint,
                params={
                    "newsId": announcement.source_id,
                    "maxRows_rsResult": "30",
                    "fh": "900",
                    "bid": "0",
                    "uid": config.uid,
                },
            )
            return parse_detail_page_html(detail_html, announcement, config.root_path)
        except Exception as exc:
            self.logger.warning(
                "Failed to fetch detail HTML for announcement %s: %s",
                announcement.source_id,
                exc,
            )
            return announcement

    async def probe(self) -> ScraperProbeResult:
        config = await self.get_widget_config(force_refresh=True)
        page = await self.fetch_page(page_num=0, max_rows=1)
        latest_title = page.announcements[0].title if page.announcements else None
        return ScraperProbeResult(
            latest_title=latest_title,
            total_pages=page.total_pages,
            widget_uid=config.uid,
        )

    async def _request_text(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> str:
        response = await self._request(method=method, url=url, params=params, data=data)
        return await response.text(encoding="utf-8", errors="ignore")

    async def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> Any:
        response = await self._request(method=method, url=url, params=params, data=data)
        return await response.json(content_type=None)

    async def _request(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> aiohttp.ClientResponse:
        return await self._request_with_retry(
            method=method,
            url=url,
            params=params,
            data=data,
            ssl_context=None,
        )

    async def _request_with_retry(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, str] | None,
        data: dict[str, str] | None,
        ssl_context: ssl.SSLContext | None,
    ) -> aiohttp.ClientResponse:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {"User-Agent": self.user_agent}
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                response = await self.session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=timeout,
                    ssl=ssl_context,
                )
                response.raise_for_status()
                return response
            except aiohttp.ClientError as exc:
                if ssl_context is None and self._should_use_insecure_ssl_fallback(url, exc):
                    self.logger.warning(
                        "TLS certificate verification failed for %s. Retrying with insecure SSL fallback for the trusted school host.",
                        url,
                    )
                    return await self._request_with_retry(
                        method=method,
                        url=url,
                        params=params,
                        data=data,
                        ssl_context=self._insecure_ssl_context,
                    )
                last_error = exc
                if attempt == 2:
                    break
                delay_seconds = 2**attempt
                self.logger.warning(
                    "Request failed for %s %s on attempt %s/3: %s",
                    method,
                    url,
                    attempt + 1,
                    exc,
                )
                await asyncio.sleep(delay_seconds)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                delay_seconds = 2**attempt
                self.logger.warning(
                    "Request failed for %s %s on attempt %s/3: %s",
                    method,
                    url,
                    attempt + 1,
                    exc,
                )
                await asyncio.sleep(delay_seconds)

        raise RuntimeError(f"request failed for {url}: {last_error}")

    def _should_use_insecure_ssl_fallback(
        self,
        url: str,
        exc: aiohttp.ClientError,
    ) -> bool:
        if not self.allow_insecure_ssl_fallback:
            return False

        request_host = (urlparse(url).hostname or "").lower()
        if not request_host or request_host != self._trusted_host:
            return False

        if isinstance(exc, aiohttp.ClientConnectorCertificateError):
            return True
        cause = getattr(exc, "__cause__", None)
        if isinstance(cause, ssl.SSLCertVerificationError):
            return True
        if isinstance(exc, aiohttp.ClientSSLError):
            ssl_errors = getattr(exc, "args", ())
            return any(
                isinstance(item, ssl.SSLCertVerificationError)
                or "certificate verify failed" in str(item).lower()
                for item in ssl_errors
            )
        return False

    @staticmethod
    def _build_insecure_ssl_context() -> ssl.SSLContext:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context