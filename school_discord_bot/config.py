from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    discord_token: str
    guild_id: int
    announcement_forum_channel_id: int
    poll_interval_seconds: int
    school_home_url: str
    school_news_widget_url: str
    database_path: Path
    dry_run: bool
    allow_insecure_school_ssl_fallback: bool
    http_timeout_seconds: int = 20
    max_backfill_count: int = 30
    max_preview_count: int = 10
    default_fetch_page_size: int = 10
    user_agent: str = (
        "SchoolDiscordBot/0.1 (+https://www.dali.tc.edu.tw/home; "
        "contact: server-admin)"
    )

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        required = {
            "DISCORD_TOKEN": os.getenv("DISCORD_TOKEN"),
            "GUILD_ID": os.getenv("GUILD_ID"),
            "ANNOUNCEMENT_FORUM_CHANNEL_ID": os.getenv("ANNOUNCEMENT_FORUM_CHANNEL_ID"),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            missing_list = ", ".join(missing)
            raise ValueError(f"missing required environment variables: {missing_list}")

        poll_interval_seconds = int(os.getenv("POLL_INTERVAL_SECONDS", "600"))
        if poll_interval_seconds < 60:
            raise ValueError("POLL_INTERVAL_SECONDS must be at least 60 seconds")

        database_path = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))

        return cls(
            discord_token=required["DISCORD_TOKEN"] or "",
            guild_id=int(required["GUILD_ID"] or "0"),
            announcement_forum_channel_id=int(
                required["ANNOUNCEMENT_FORUM_CHANNEL_ID"] or "0"
            ),
            poll_interval_seconds=poll_interval_seconds,
            school_home_url=os.getenv("SCHOOL_HOME_URL", "https://www.dali.tc.edu.tw/home"),
            school_news_widget_url=os.getenv(
                "SCHOOL_NEWS_WIDGET_URL",
                "https://www.dali.tc.edu.tw/ischool/widget/site_news/main2.php?allbtn=0&maximize=1&uid=WID_0_2_377afa59cce9f22276e3f66e9d896cb97110c95d",
            ),
            database_path=database_path,
            dry_run=_parse_bool(os.getenv("DRY_RUN"), default=False),
            allow_insecure_school_ssl_fallback=_parse_bool(
                os.getenv("ALLOW_INSECURE_SCHOOL_SSL_FALLBACK"),
                default=True,
            ),
        )

    def resolve_database_path(self, project_root: Path) -> Path:
        if self.database_path.is_absolute():
            return self.database_path
        return project_root / self.database_path