from school_discord_bot.models.announcement import build_source_hash


def test_source_hash_prefers_canonical_url() -> None:
    first = build_source_hash(
        "https://example.com/news?nid=1&utm_source=discord",
        date="2026/06/23",
        category="一般公告",
        unit="教學組",
        title="測試公告",
    )
    second = build_source_hash(
        "https://example.com/news?nid=1",
        date="2026/06/23",
        category="一般公告",
        unit="教學組",
        title="不同內容也應以 canonical url 視為同一筆",
    )
    assert first == second


def test_source_hash_falls_back_to_content_signature() -> None:
    first = build_source_hash(
        None,
        date="2026/06/23",
        category="一般公告",
        unit="教學組",
        title="測試公告",
    )
    second = build_source_hash(
        None,
        date="2026/06/23",
        category="一般公告",
        unit="教學組",
        title="測試公告",
    )
    third = build_source_hash(
        None,
        date="2026/06/24",
        category="一般公告",
        unit="教學組",
        title="測試公告",
    )
    assert first == second
    assert first != third