from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from school_discord_bot.models.curriculum import (
    PERIOD_START,
    WEEKDAY_NAMES,
    ClassTimetable,
    NowStatus,
    PeriodState,
    format_period_range,
)

logger = logging.getLogger(__name__)

# Layout constants (all in pixels at render scale; we render at 2× and
# downscale for crisp text).
_CELL_W = 200
_CELL_H = 72
_LABEL_W = 80
_HEADER_H = 52
_PADDING = 10
_SCALE = 2

# Colors.
_BG = (255, 255, 255)
_GRID_LINE = (210, 210, 210)
_HEADER_BG = (240, 240, 240)
_TEXT = (30, 30, 30)
_SUB_TEXT = (120, 120, 120)
_TODAY_BG = (227, 242, 253)  # light blue
_TODAY_LABEL = (25, 118, 210)
_NOW_BORDER = (211, 47, 47)  # red
_NOW_ARROW = (211, 47, 47)

# Font cache, resolved lazily.
_font_cache: tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont, ImageFont.FreeTypeFont] | None = None

_FONT_PATH_CANDIDATES: tuple[str, ...] = (
    # Windows — both confirmed present on this dev host.
    r"C:\Windows\Fonts\msjh.ttc",
    r"C:\Windows\Fonts\NotoSansTC-VF.ttf",
    # Debian/Ubuntu (apt install fonts-noto-cjk).
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    # Fallback — the old-fashioned way on linux.
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
)


def _resolve_font_path() -> str | None:
    """Walk candidate paths and the ``CURRICULUM_FONT_PATH`` env var."""
    import os

    env = os.getenv("CURRICULUM_FONT_PATH")
    if env and Path(env).is_file():
        return env

    for cand in _FONT_PATH_CANDIDATES:
        if Path(cand).is_file():
            logger.debug("Using font: %s", cand)
            return cand

    return None


def _ensure_fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    global _font_cache
    if _font_cache is not None:
        return _font_cache

    font_path = _resolve_font_path()
    if font_path is None:
        raise RuntimeError(
            "No CJK font found. Install fonts-noto-cjk in the container, or set "
            "CURRICULUM_FONT_PATH to a .ttf/.ttc/.otf file with Traditional Chinese glyphs."
        )
    # Pillow can handle .ttc via the index= kwarg.  For most CJK .ttc files index 0
    # is the regular weight which is what we want.
    (subject_font, teacher_font, label_font) = (
        ImageFont.truetype(font_path, _SCALE * 16),
        ImageFont.truetype(font_path, _SCALE * 12),
        ImageFont.truetype(font_path, _SCALE * 14),
    )
    _font_cache = (subject_font, teacher_font, label_font)
    return _font_cache


def render_week_image(
    timetable: ClassTimetable,
    *,
    now: NowStatus | None = None,
    today: int | None = None,
) -> io.BytesIO:
    """Draw a full-week Mon–Fri grid into a PNG ``BytesIO``."""
    subject_font, teacher_font, label_font = _ensure_fonts()
    max_p = max(timetable.max_period(), 1)

    cols = len(WEEKDAY_NAMES)  # 5
    total_w = _LABEL_W + cols * _CELL_W
    total_h = _HEADER_H + max_p * _CELL_H
    img = Image.new("RGB", (total_w * _SCALE, total_h * _SCALE), _BG)
    draw = ImageDraw.Draw(img)

    # ------------------------------------------------------------------
    # Header row
    # ------------------------------------------------------------------
    _rect(draw, 0, 0, total_w, _HEADER_H, fill=_HEADER_BG)
    _text_center(draw, f"節 / 時間", _LABEL_W // 2, _HEADER_H // 2, label_font, _TEXT)
    for ci, name in enumerate(WEEKDAY_NAMES):
        x = _LABEL_W + ci * _CELL_W + _CELL_W // 2
        color = _TODAY_LABEL if ci == today else _TEXT
        _text_center(draw, name, x, _HEADER_H // 2, label_font, color)

    # Today-column background tint.
    if today is not None and 0 <= today < cols:
        _rect(
            draw,
            _LABEL_W + today * _CELL_W,
            _HEADER_H,
            _CELL_W,
            max_p * _CELL_H,
            fill=_TODAY_BG,
        )

    # ------------------------------------------------------------------
    # Period rows
    # ------------------------------------------------------------------
    for period in range(1, max_p + 1):
        y = _HEADER_H + (period - 1) * _CELL_H

        # Horizontal grid line.
        draw.line([(0, y * _SCALE), (total_w * _SCALE, y * _SCALE)], fill=_GRID_LINE, width=_SCALE)

        # Row label.
        start = PERIOD_START.get(period)
        label_lines = [f"第 {period} 節"]
        range_text = format_period_range(period)
        if range_text:
            label_lines.append(range_text)
        else:
            label_lines.append("(無時間)")
        text_y = y + _CELL_H // 2 - (len(label_lines) - 1) * 18
        for li, line in enumerate(label_lines):
            _text_center(draw, line, _LABEL_W // 2, text_y + li * 36, label_font, _TEXT)

        # Current-period marker.
        if now is not None and now.state is PeriodState.IN_PERIOD and now.period == period:
            _text_center(draw, "▶", _LABEL_W // 2, text_y - 24, label_font, _NOW_ARROW, scale=1.5)

        # Vertical grid lines between days.
        for ci in range(1, cols):
            dx = _LABEL_W + ci * _CELL_W
            draw.line(
                [(dx * _SCALE, y * _SCALE), (dx * _SCALE, (y + _CELL_H) * _SCALE)],
                fill=_GRID_LINE,
                width=_SCALE,
            )

        # Cells.
        for ci in range(cols):
            cx = _LABEL_W + ci * _CELL_W
            lesson = timetable.lesson_at(ci, period)

            # Current-period red border.
            if (
                now is not None
                and now.state is PeriodState.IN_PERIOD
                and now.period == period
            ):
                _border(draw, cx, y, _CELL_W, _CELL_H, _NOW_BORDER, width=3)

            if lesson is None:
                continue

            # Subject.
            subject = _truncate(lesson.subject, 12, subject_font)
            _text_center(draw, subject, cx + _CELL_W // 2, y + 18, subject_font, _TEXT)

            # Teacher(s).
            if lesson.teachers:
                t_text = "、".join(lesson.teachers)
                t_text = _truncate(t_text, 16, teacher_font)
                _text_center(draw, t_text, cx + _CELL_W // 2, y + _CELL_H // 2 + 8, teacher_font, _SUB_TEXT)

    # Bottom border.
    draw.line(
        [(0, (total_h - 1) * _SCALE), (total_w * _SCALE, (total_h - 1) * _SCALE)],
        fill=_GRID_LINE,
        width=_SCALE,
    )

    # Downscale.
    img = img.resize((total_w, total_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ------------------------------------------------------------------
# Drawing helpers (operate at _SCALE × coordinates)
# ------------------------------------------------------------------

def _rect(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    fill: tuple[int, int, int],
) -> None:
    draw.rectangle([(x * _SCALE, y * _SCALE), ((x + w) * _SCALE, (y + h) * _SCALE)], fill=fill)


def _border(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    color: tuple[int, int, int],
    *,
    width: int = 2,
) -> None:
    sw = width * _SCALE
    sx, sy = x * _SCALE, y * _SCALE
    ex, ey = (x + w) * _SCALE, (y + h) * _SCALE
    draw.rectangle([(sx, sy), (ex, sy + sw)], fill=color)
    draw.rectangle([(sx, ey - sw), (ex, ey)], fill=color)
    draw.rectangle([(sx, sy), (sx + sw, ey)], fill=color)
    draw.rectangle([(ex - sw, sy), (ex, ey)], fill=color)


def _text_center(
    draw: ImageDraw.ImageDraw,
    text: str,
    cx: int,
    cy: int,
    font: ImageFont.FreeTypeFont,
    color: tuple[int, int, int],
    *,
    scale: float = 1.0,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = int(cx * _SCALE - tw * scale / 2)
    y = int(cy * _SCALE - th * scale / 2)
    if scale != 1.0:
        # Pillow doesn't support fractional scaling natively — render on a
        # temporary image and paste it back.
        tmp = Image.new("RGBA", (int(tw * scale + 2), int(th * scale + 2)), (0, 0, 0, 0))
        tmp_draw = ImageDraw.Draw(tmp)
        tmp_draw.text((1, 1), text, font=font, fill=color)
        resized = tmp.resize((int(tw * scale), int(th * scale)), Image.LANCZOS)
        draw._image.paste(resized, (x, y), resized)
    else:
        draw.text((x, y), text, font=font, fill=color)


def _truncate(text: str, max_chars: int, font: ImageFont.FreeTypeFont) -> str:
    if len(text) <= max_chars:
        return text
    trial = text[: max_chars - 1]
    # Iterative shrink: some CJK chars are full-width and need less text.
    cell_w = _CELL_W - 2 * _PADDING
    while trial:
        bbox = font.getbbox(trial + "…")
        w = bbox[2] - bbox[0]
        if w <= cell_w * _SCALE:
            return trial + "…"
        trial = trial[:-1]
    return "…"
