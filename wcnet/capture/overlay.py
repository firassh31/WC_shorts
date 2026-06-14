"""Simple event-text overlay (no scoreboard).

The raw broadcast already carries its own scoreboard, so we add only a single,
clean caption describing the event — e.g. "GOAL - GERMANY" or "RED CARD" —
positioned in the top ~15% of the 9:16 frame so it never obstructs the action.
Plain white text with a thin black stroke + soft shadow for readability against
bright backgrounds. Rendered as a transparent PNG the clipper overlays in ffmpeg.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..models import EventType, Fixture, MatchEvent

W, H = 1080, 1920
_TOP_Y = int(H * 0.085)          # baseline within the top 15% band
_MAX_TEXT_W = W - 120            # keep margins; shrink font to fit

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_font_cache: dict[int, ImageFont.FreeTypeFont] = {}

_EVENT_WORD: dict[EventType, str] = {
    EventType.GOAL: "GOAL",
    EventType.PENALTY: "PENALTY",
    EventType.RED_CARD: "RED CARD",
    EventType.YELLOW_CARD: "YELLOW CARD",
    EventType.VAR: "VAR",
    EventType.SCREAMER: "SCREAMER",
    EventType.GREAT_SAVE: "GREAT SAVE",
    EventType.NEAR_MISS: "SO CLOSE",
    EventType.SKILL: "SKILL",
    EventType.HIGHLIGHT: "HIGHLIGHT",
}
# Event types that read well with the team appended.
_WITH_TEAM = {EventType.GOAL, EventType.PENALTY, EventType.RED_CARD}


def _font(size: int) -> ImageFont.FreeTypeFont:
    if size in _font_cache:
        return _font_cache[size]
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            f = ImageFont.truetype(path, size)
            _font_cache[size] = f
            return f
    f = ImageFont.load_default()
    _font_cache[size] = f
    return f


def event_label(fixture: Fixture, event: MatchEvent) -> str:
    word = _EVENT_WORD.get(event.event_type, event.event_type.value.upper())
    team = (event.team or "").strip()
    if event.event_type in _WITH_TEAM and team:
        return f"{word} - {team.upper()}"
    return word


def build_event_overlay_png(out_path: Path, text: str) -> Path:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Shrink to fit within the safe width.
    size = 96
    font = _font(size)
    while d.textlength(text, font=font) > _MAX_TEXT_W and size > 40:
        size -= 4
        font = _font(size)

    cx = W // 2
    # Soft shadow first, then white text with a thin black stroke.
    d.text((cx + 4, _TOP_Y + 4), text, font=font, fill=(0, 0, 0, 140), anchor="mm")
    d.text((cx, _TOP_Y), text, font=font, fill=(255, 255, 255, 255), anchor="mm",
           stroke_width=4, stroke_fill=(0, 0, 0, 235))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def event_overlay_for(
    out_path: Path, fixture: Fixture, event: MatchEvent,
    home_score: int | None = None, away_score: int | None = None,
) -> Path:
    """Build the simple event-text overlay PNG for a fixture+event.

    ``home_score``/``away_score`` are accepted for call-compatibility but are
    intentionally unused — we do not draw a scoreboard.
    """
    return build_event_overlay_png(out_path, event_label(fixture, event))


# Back-compat alias for existing call sites (was a scoreboard, now plain text).
scorebug_for_event = event_overlay_for
