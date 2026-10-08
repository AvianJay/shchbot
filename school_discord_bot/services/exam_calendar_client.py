from __future__ import annotations

import asyncio
from datetime import date, datetime
import json
import logging

import aiohttp

from school_discord_bot.db.database import Database
from school_discord_bot.models.curriculum import TAIPEI_TZ
from school_discord_bot.models.exam import ExamSchedule, ExamType, parse_exam_date


EXAM_CALENDAR_URL = "https://raw.githubusercontent.com/AvianJay/taiwan-exam-calendar/refs/heads/main/data/exams.json"
CACHE_SETTING_KEY = "exam_calendar_cache"
logger = logging.getLogger(__name__)


class ExamCalendarError(ValueError):
    pass


def parse_exam_calendar(payload: object) -> tuple[ExamSchedule, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise ExamCalendarError("考試日曆資料格式不正確。")
    exam_codes = {kind.source_code: kind for kind in ExamType}
    schedules: list[ExamSchedule] = []
    for event in payload["events"]:
        if not isinstance(event, dict) or event.get("type") != "exam":
            continue
        code = event.get("exam")
        exam_type = exam_codes.get(code) if isinstance(code, str) else None
        if exam_type is None:
            continue
        try:
            start = parse_exam_date(event.get("start_date"), exam_type=exam_type)
            end = parse_exam_date(event.get("end_date") or event.get("start_date"), exam_type=exam_type)
            if end < start:
                raise ValueError("Exam ends before it starts")
        except ValueError:
            logger.warning("Skipping an invalid %s exam calendar event", exam_type.name)
            continue
        schedules.append(ExamSchedule(exam_type, start, end))
    if not schedules:
        raise ExamCalendarError("考試日曆尚未提供有效的學測或分科考試資料。")
    return tuple(schedules)


def select_exam_date(schedules: tuple[ExamSchedule, ...], exam_type: ExamType, today: date) -> date:
    candidates = [schedule for schedule in schedules if schedule.exam_type == exam_type]
    if not candidates:
        raise ExamCalendarError(f"考試日曆尚未提供{exam_type}考試日期，請待來源更新後重試。")
    upcoming = [schedule for schedule in candidates if schedule.end_date >= today]
    # Keep a multi-day exam at zero until it ends; switch to the next available
    # year afterwards. If the next year is unpublished, keep the latest at zero.
    selected = (
        min(upcoming, key=lambda item: item.start_date)
        if upcoming else max(candidates, key=lambda item: item.start_date)
    )
    return selected.start_date


class ExamCalendarClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        database: Database,
        timeout_seconds: int,
        user_agent: str,
    ) -> None:
        self.session = session
        self.database = database
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.headers = {"User-Agent": user_agent}
        self._schedules: tuple[ExamSchedule, ...] | None = None
        self._refresh_lock = asyncio.Lock()

    async def refresh(self) -> None:
        async with self._refresh_lock:
            try:
                async with self.session.get(EXAM_CALENDAR_URL, timeout=self.timeout, headers=self.headers) as response:
                    response.raise_for_status()
                    # GitHub Raw serves JSON with a text/plain content type.
                    schedules = parse_exam_calendar(await response.json(content_type=None))
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                logger.warning("Exam calendar refresh failed (%s); trying saved data", type(exc).__name__)
                if self._schedules is None:
                    cached = await self.database.get_setting(CACHE_SETTING_KEY)
                    if cached is not None:
                        try:
                            self._schedules = parse_exam_calendar(json.loads(cached))
                        except ValueError:
                            logger.warning("Saved exam calendar data is invalid")
                if self._schedules is None:
                    raise ExamCalendarError("暫時無法取得考試日曆，且沒有可用快取；請稍後重試。") from exc
                return

            self._schedules = schedules
            await self.database.set_setting(
                CACHE_SETTING_KEY,
                json.dumps({"events": [
                    {"type": "exam", "exam": item.exam_type.source_code,
                     "start_date": item.start_date.isoformat(), "end_date": item.end_date.isoformat()}
                    for item in schedules
                ]}),
            )

    async def get_exam_date(self, exam_type: ExamType, *, today: date | None = None) -> date:
        if self._schedules is None:
            await self.refresh()
        assert self._schedules is not None
        return select_exam_date(self._schedules, exam_type, today or datetime.now(TAIPEI_TZ).date())
