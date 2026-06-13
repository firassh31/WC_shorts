"""Event-driven scorebug overlay (basic text style).

Renders a transparent PNG banner — event label, score/teams, minute — that the
clipper composites onto the top of the 9:16 clip. Pillow gives robust text
handling (any characters, clean spacing) regardless of footage source.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..models import EventType, Fixture, MatchEvent

WIDTH = 1080
BAND_H = 196

_FONT_CANDIDATES = {
    "bold": ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "regular": ["C:/Windows/Fonts/arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
}
_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

# Short, clean labels per event type for the left badge.
_EVENT_LABEL: dict[EventType, str] = {
    EventType.GOAL: "GOAL",
    EventType.PENALTY: "PENALTY",
    EventType.RED_CARD: "RED CARD",
    EventType.YELLOW_CARD: "BOOKING",
    EventType.VAR: "VAR",
    EventType.SCREAMER: "SCREAMER",
    EventType.GREAT_SAVE: "SAVE",
    EventType.NEAR_MISS: "CLOSE!",
    EventType.SKILL: "SKILL",
    EventType.HIGHLIGHT: "HIGHLIGHT",
}

_ACCENT: dict[EventType, tuple[int, int, int]] = {
    EventType.GOAL: (0, 200, 110),
    EventType.PENALTY: (255, 170, 0),
    EventType.RED_CARD: (230, 40, 40),
    EventType.YELLOW_CARD: (240, 200, 0),
    EventType.VAR: (90, 160, 255),
    EventType.SCREAMER: (255, 90, 40),
    EventType.GREAT_SAVE: (0, 190, 200),
    EventType.NEAR_MISS: (255, 120, 0),
    EventType.SKILL: (180, 110, 255),
    EventType.HIGHLIGHT: (0, 180, 255),
}

_ABBR = {
    "Argentina": "ARG", "France": "FRA", "Brazil": "BRA", "Croatia": "CRO",
    "Morocco": "MAR", "Portugal": "POR", "England": "ENG", "Spain": "ESP",
    "Germany": "GER", "Netherlands": "NED", "Belgium": "BEL", "Italy": "ITA",
}


def team_abbr(name: str) -> str:
    if name in _ABBR:
        return _ABBR[name]
    letters = "".join(c for c in name.upper() if c.isalpha())
    return letters[:3] or "TBD"


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    key = (kind, size)
    if key in _font_cache:
        return _font_cache[key]
    for path in _FONT_CANDIDATES.get(kind, []):
        if Path(path).exists():
            f = ImageFont.truetype(path, size)
            _font_cache[key] = f
            return f
    f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def build_scorebug_png(
    out_path: Path,
    *,
    event_label: str,
    center_text: str,
    minute_text: str,
    accent: tuple[int, int, int],
) -> Path:
    img = Image.new("RGBA", (WIDTH, BAND_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    x0, x1, y0, y1 = 40, WIDTH - 40, 30, BAND_H - 26
    d.rounded_rectangle((x0, y0, x1, y1), radius=30, fill=(10, 12, 20, 210))
    # Accent bar on the left edge.
    d.rounded_rectangle((x0, y0, x0 + 16, y1), radius=8, fill=(*accent, 255))
    cy = (y0 + y1) // 2

    # Left: event label (accent colour).
    d.text((x0 + 42, cy), event_label, font=_font("bold", 44),
           fill=(*accent, 255), anchor="lm")

    # Center: score / teams.
    d.text((WIDTH / 2, cy), center_text, font=_font("bold", 60),
           fill=(255, 255, 255, 255), anchor="mm")

    # Right: minute pill.
    if minute_text:
        mf = _font("bold", 44)
        tw = d.textlength(minute_text, font=mf)
        pad = 26
        px1 = x1 - 30
        px0 = px1 - tw - pad * 2
        d.rounded_rectangle((px0, cy - 38, px1, cy + 38), radius=38,
                            fill=(*accent, 255))
        d.text(((px0 + px1) / 2, cy), minute_text, font=mf,
               fill=(10, 12, 20, 255), anchor="mm")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def scorebug_for_event(
    out_path: Path, fixture: Fixture, event: MatchEvent,
    home_score: int | None = None, away_score: int | None = None,
) -> Path:
    """Build the scorebug PNG straight from a fixture + detected event."""
    ha = team_abbr(fixture.home_team) if fixture.home_team else "HOME"
    aa = team_abbr(fixture.away_team) if fixture.away_team else "AWAY"
    if home_score is not None and away_score is not None:
        center = f"{ha}  {home_score} - {away_score}  {aa}"
    else:
        center = f"{ha}  vs  {aa}"
    minute = f"{event.minute}'" if event.minute is not None else ""
    label = _EVENT_LABEL.get(event.event_type, event.event_type.value.upper())
    accent = _ACCENT.get(event.event_type, (0, 180, 255))
    return build_scorebug_png(
        out_path, event_label=label, center_text=center,
        minute_text=minute, accent=accent,
    )
