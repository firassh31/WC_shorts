"""Dynamic caption & hashtag generation tailored to each event type.

Injected into every platform's metadata so the syndicated posts carry the
right match context and high-visibility, event-specific hashtags.
"""

from __future__ import annotations

from .models import EventType, Fixture, MatchEvent

# Per-event-type templates and tag bundles.
_HEADLINES: dict[EventType, str] = {
    EventType.GOAL: "GOAL! ⚽🔥",
    EventType.PENALTY: "PENALTY DRAMA! 🎯",
    EventType.RED_CARD: "RED CARD! 🟥",
    EventType.YELLOW_CARD: "Booked! 🟨",
    EventType.VAR: "VAR CHECK! 📺",
    EventType.SCREAMER: "WHAT A SCREAMER! 🚀",
    EventType.GREAT_SAVE: "UNBELIEVABLE SAVE! 🧤",
    EventType.NEAR_MISS: "SO CLOSE! 😱",
    EventType.SKILL: "OUTRAGEOUS SKILL! ✨",
    EventType.HIGHLIGHT: "HUGE MOMENT! 🔥",
}

_EVENT_TAGS: dict[EventType, list[str]] = {
    EventType.GOAL: ["#goal", "#golazo"],
    EventType.PENALTY: ["#penalty", "#spotkick"],
    EventType.RED_CARD: ["#redcard", "#sentoff"],
    EventType.YELLOW_CARD: ["#yellowcard"],
    EventType.VAR: ["#var", "#controversy"],
    EventType.SCREAMER: ["#screamer", "#worldie", "#golazo"],
    EventType.GREAT_SAVE: ["#save", "#goalkeeper", "#worldclass"],
    EventType.NEAR_MISS: ["#soclose", "#offthepost"],
    EventType.SKILL: ["#skills", "#football", "#magic"],
    EventType.HIGHLIGHT: ["#highlights", "#football"],
}

_BASE_TAGS = ["#Shorts", "#WorldCup", "#Football", "#Soccer", "#FIFA", "#fyp"]


def build_caption(fixture: Fixture, event: MatchEvent) -> str:
    headline = _HEADLINES.get(event.event_type, "Big moment!")
    minute = f"{event.minute}'" if event.minute is not None else ""
    actor = ""
    if event.player and event.team:
        actor = f" — {event.player} ({event.team})"
    elif event.team:
        actor = f" — {event.team}"
    context = f"{fixture.home_team} vs {fixture.away_team}"
    round_bit = f" · {fixture.round_name}" if fixture.round_name else ""
    parts = [
        f"{headline} {minute}{actor}".strip(),
        f"{context}{round_bit} · {fixture.league_name}",
    ]
    return "\n".join(p for p in parts if p)


def build_hashtags(fixture: Fixture, event: MatchEvent) -> list[str]:
    def teamtag(name: str) -> str:
        return "#" + "".join(c for c in name if c.isalnum())

    tags: list[str] = list(_BASE_TAGS)
    tags += _EVENT_TAGS.get(event.event_type, [])
    tags += [teamtag(fixture.home_team), teamtag(fixture.away_team)]
    # De-dupe while preserving order; cap to a sane number for the platforms.
    seen: set[str] = set()
    unique: list[str] = []
    for tag in tags:
        low = tag.lower()
        if low not in seen and len(tag) > 1:
            seen.add(low)
            unique.append(tag)
    return unique[:18]
