from __future__ import annotations

from pathlib import Path

from school_discord_bot.models.announcement import Announcement
from school_discord_bot.services.announcement_parser import (
    build_public_news_url,
    parse_announcements_from_table_html,
    parse_detail_json,
    parse_detail_page_html,
    parse_list_json,
    parse_widget_config,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"
WIDGET_URL = "https://www.dali.tc.edu.tw/ischool/widget/site_news/main2.php?allbtn=0&maximize=1&uid=WID_0_2_377afa59cce9f22276e3f66e9d896cb97110c95d"


def test_parse_widget_config_and_table_rows() -> None:
    html = (FIXTURES_DIR / "sample_news_page.html").read_text(encoding="utf-8")
    config = parse_widget_config(html, WIDGET_URL)

    assert config.uid == "WID_0_2_377afa59cce9f22276e3f66e9d896cb97110c95d"
    assert config.list_endpoint.endswith("news_query_json.php")
    assert config.detail_endpoint.endswith("news_query_json_content.php")

    announcements = parse_announcements_from_table_html(html, config)
    assert len(announcements) == 1
    assert announcements[0].title == "114學年度校內GIS營隊錄取名單及營隊資訊"
    assert announcements[0].category == "課程活動"
    assert announcements[0].unit == "教學組"
    assert announcements[0].source_url == build_public_news_url(config.public_view_base_url, "19907")


def test_parse_list_json_and_detail_json() -> None:
    config = parse_widget_config(
        (FIXTURES_DIR / "sample_news_page.html").read_text(encoding="utf-8"),
        WIDGET_URL,
    )
    list_payload = [
        {"pageNum": 0, "maxRows": 5, "totalPages": 10},
        {
            "newsId": "19907",
            "time": "2026/06/23",
            "attr_name": "課程活動",
            "title": "114學年度校內GIS營隊錄取名單及營隊資訊",
            "title_hint": "114學年度校內GIS營隊錄取名單及營隊資訊",
            "unit_name": "教學組",
            "clicks": "71",
            "content_type": "content",
            "content": None,
        },
    ]

    page = parse_list_json(list_payload, config)
    assert page.total_pages == 10
    assert len(page.announcements) == 1

    detail_payload = [
        {
            "rcode": 200,
            "newsId": "19907",
            "time": "2026-06-23 16:50:12",
            "unit": "教學組",
            "content": "%3Cp%3E%E5%A0%B1%E5%90%8D%E6%9C%9F%E9%99%90%EF%BC%9A115%2F07%2F12%28%E6%97%A5%29%E6%88%AA%E6%AD%A2%E3%80%82%3C%2Fp%3E%3Cp%3E%3Ca%20href%3D%22https%3A%2F%2Fexample.com%2Fform%22%3E%E7%B7%9A%E4%B8%8A%E5%A0%B1%E5%90%8D%E8%A1%A8%3C%2Fa%3E%3C%2Fp%3E",
            "attachedfile": '[[1,2048,"%u5831%u540D%u7C21%u7AE0.pdf"]]',
        }
    ]

    enriched = parse_detail_json(detail_payload, page.announcements[0], config.root_path)
    assert enriched.excerpt == "報名期限：115/07/12(日)截止。 線上報名表"
    assert enriched.attachments[0].name == "報名簡章.pdf"
    assert enriched.external_links[0].url == "https://example.com/form"
    assert enriched.important_dates == ["115/07/12(日)"]


def test_parse_detail_page_html_fixture() -> None:
    config = parse_widget_config(
        (FIXTURES_DIR / "sample_news_page.html").read_text(encoding="utf-8"),
        WIDGET_URL,
    )
    base_announcement = Announcement(
        source_id="19907",
        source_hash="hash",
        source_url=build_public_news_url(config.public_view_base_url, "19907"),
        title="114學年度校內GIS營隊錄取名單及營隊資訊",
        date="2026/06/23",
        category="課程活動",
        unit="教學組",
    )

    html = (FIXTURES_DIR / "sample_news_detail.html").read_text(encoding="utf-8")
    parsed = parse_detail_page_html(html, base_announcement, config.root_path)

    assert parsed.unit == "教學組"
    assert parsed.attachments[0].name == "報名簡章.pdf"
    assert parsed.external_links[0].label == "線上報名表"
    assert parsed.important_dates == ["115/07/12(日)"]