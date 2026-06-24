from __future__ import annotations

from discord import Locale, app_commands


class CommandTranslator(app_commands.Translator):
    """Provide a stable zh-TW command translation layer for slash command metadata."""

    async def translate(
        self,
        string: app_commands.locale_str,
        locale: Locale,
        context: app_commands.TranslationContext,
    ) -> str | None:
        if locale is not Locale.american_english:
            return None

        translations = {
            "school": "school",
            "news": "news",
            "學校網站與實用連結指令": "School site and utility commands",
            "學校公告同步與查詢指令": "School announcement sync and search commands",
            "setup": "setup",
            "檢查 bot、論壇頻道、資料庫與爬蟲狀態": "Check bot, forum channel, database, and scraper status",
            "links": "links",
            "顯示學校常用公開連結": "Show common public school links",
            "help": "help",
            "顯示可用指令說明": "Show available command help",
            "latest": "latest",
            "查詢最近的校內公告": "Show recent school announcements",
            "search": "search",
            "搜尋已保存的公告": "Search saved announcements",
            "check": "check",
            "立即檢查最新公告並同步": "Check latest announcements and sync now",
            "backfill": "backfill",
            "補發 bot 啟用前的最新公告": "Backfill recent announcements before bot startup",
            "dry_run": "dry_run",
            "預覽最新公告但不實際發文": "Preview latest announcements without posting",
            "status": "status",
            "查看公告同步狀態": "Show announcement sync status",
            "sync_tags": "sync_tags",
            "建立或同步學校公告類別標籤": "Create or sync school announcement tags",
            "tag_map": "tag_map",
            "手動指定學校類別對應的論壇標籤": "Manually map a school category to a forum tag",
            "count": "count",
            "要顯示幾筆公告，預設 5，最多 10": "How many announcements to show, default 5, max 10",
            "category": "category",
            "限定類別": "Filter by category",
            "unit": "unit",
            "限定單位": "Filter by unit",
            "keyword": "keyword",
            "限定關鍵字": "Filter by keyword",
            "搜尋關鍵字": "Search keyword",
            "要補發幾筆公告，預設 5，最多 30": "How many announcements to backfill, default 5, max 30",
            "要預覽幾筆公告，預設 5，最多 10": "How many announcements to preview, default 5, max 10",
            "學校公告類別": "School announcement category",
            "現有論壇標籤名稱或 ID": "Existing forum tag name or ID",
            "tag": "tag",
        }
        return translations.get(str(string))