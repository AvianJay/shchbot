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
            # Curriculum
            "課表": "timetable",
            "查詢班級今日課表": "Look up today's class timetable",
            "班級": "class_code",
            "班級代號，例如 205": "Class code, e.g. 205",
            "班級代號，例如 205（不填則查詢你的班級）": "Class code, e.g. 205 (leave blank for your saved class)",
            "send_curriculum": "send_curriculum",
            "將班級課表查詢面板發送到頻道": "Post a class timetable lookup panel to the channel",
            # Exam countdown
            "countdown": "countdown",
            "設定學測倒數語音頻道，每天台灣時間 00:00 更新名稱": "Set a GSAT countdown voice channel, updated daily at Taipei midnight",
            "channel": "channel",
            "exam_date": "exam_date",
            "顯示學測倒數的語音頻道": "Voice channel for the GSAT countdown",
            "學測第一天的日期，格式 YYYY-MM-DD": "First day of the GSAT exam, in YYYY-MM-DD format",
            # Anonymous board
            "anon": "anon",
            "匿名版管理指令": "Anonymous board admin commands",
            "設定匿名版的匿名頻道、後台頻道與是否需要審核": "Set the anonymous board's public channel, review channel, and review mode",
            "public_channel": "public_channel",
            "review_channel": "review_channel",
            "require_review": "require_review",
            "公開發布匿名投稿的文字頻道": "Text channel where anonymous posts are published",
            "管理員審核與紀錄用的後台頻道（會顯示投稿者）": "Staff channel for review and logs (shows who posted)",
            "是否需要管理員審核後才發布，預設為是": "Require staff approval before publishing (default: yes)",
            "查看匿名版目前的設定與待審核數量": "Show anonymous board settings and pending count",
            "send_panel": "send_panel",
            "將匿名投稿面板發送到目前頻道": "Post the anonymous submission panel to this channel",
            "管理匿名版分類": "Manage anonymous board categories",
            "add": "add",
            "新增匿名版分類": "Add an anonymous board category",
            "remove": "remove",
            "刪除匿名版分類": "Remove an anonymous board category",
            "list": "list",
            "列出匿名版分類": "List anonymous board categories",
            "name": "name",
            "分類名稱，可包含 emoji，例如「😡 我要靠北」": "Category name, may include emoji, e.g. 「😡 我要靠北」",
            "要刪除的分類名稱": "Name of the category to remove",
        }
        return translations.get(str(string))
