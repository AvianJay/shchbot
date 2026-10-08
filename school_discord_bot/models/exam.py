from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class ExamType(StrEnum):
    GSAT = "學測"
    SUBJECT = "分科"

    @property
    def source_code(self) -> str:
        return "gsat" if self is ExamType.GSAT else "ast"


def parse_exam_date(value: str, *, exam_type: ExamType = ExamType.GSAT) -> date:
    """Parse exact YYYY-MM-DD dates from the exam calendar."""
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError(f"{exam_type}日期格式須為 YYYY-MM-DD，且必須是有效日期。") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{exam_type}日期格式須為 YYYY-MM-DD，且必須是有效日期。")
    return parsed


@dataclass(frozen=True, slots=True)
class ExamSchedule:
    exam_type: ExamType
    start_date: date
    end_date: date
