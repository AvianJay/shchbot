from __future__ import annotations

import discord


PUBLIC_SCHOOL_LINKS: tuple[tuple[str, str], ...] = (
    ("成績查詢", "https://www.dali.tc.edu.tw/home?cid=1521"),
    ("班級課程查詢", "https://campus.dali.tc.edu.tw/"),
    ("學校行事曆", "https://www.dali.tc.edu.tw/ischool/publish_page/49/?cid=1307"),
    ("學習歷程上傳", "https://epf-mlife.k12ea.gov.tw/Portal.do"),
    ("Office365 教育雲", "https://o365.k12cc.tw/"),
    ("程式解題練習", "https://code.dali.tc.edu.tw"),
    ("圖書資訊查詢", "https://www.dali.tc.edu.tw/ischool/publish_page/19/"),
)


def build_school_links_embed() -> discord.Embed:
    embed = discord.Embed(
        title="興附常用連結",
        description="以下皆為學校或教育單位提供的公開入口，不會要求你把帳號密碼交給機器人。",
        color=discord.Color.blue(),
    )
    for label, url in PUBLIC_SCHOOL_LINKS:
        embed.add_field(name=label, value=url, inline=False)
    return embed


class SchoolLinksView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        for label, url in PUBLIC_SCHOOL_LINKS:
            self.add_item(discord.ui.Button(label=label, url=url))