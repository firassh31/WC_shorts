"""Dynamic caption, description-question & hashtag generation per event type.

Produces, for each detected event:
  • a punchy title line (event + scorer + minute + score),
  • an engagement line tailored to the event type (details for a goal, a debate
    question for a red card / penalty / VAR, etc.),
  • a relevant, de-duplicated hashtag set (teams, scorer, event, competition).
"""

from __future__ import annotations

from .models import EventType, Fixture, MatchEvent

_HEADLINES: dict[EventType, str] = {
    EventType.GOAL: "GOAL! ⚽🔥",
    EventType.PENALTY: "PENALTY! 🎯",
    EventType.RED_CARD: "RED CARD! 🟥",
    EventType.YELLOW_CARD: "Booked! 🟨",
    EventType.VAR: "VAR CHECK! 📺",
    EventType.SCREAMER: "WHAT A SCREAMER! 🚀",
    EventType.GREAT_SAVE: "UNBELIEVABLE SAVE! 🧤",
    EventType.NEAR_MISS: "SO CLOSE! 😱",
    EventType.SKILL: "OUTRAGEOUS SKILL! ✨",
    EventType.HIGHLIGHT: "HUGE MOMENT! 🔥",
}

# Event-specific engagement line for the description (the "question/details").
_QUESTIONS: dict[EventType, str] = {
    EventType.GOAL: "🔥 What a finish! Is this the goal of the tournament? Drop a 🐐 below.",
    EventType.PENALTY: "🎯 Penalty given — was it the right call? 👇 Let us know.",
    EventType.RED_CARD: "🟥 Straight red! Is it a deserved red card? Debate below 👇",
    EventType.YELLOW_CARD: "🟨 Yellow card — fair booking or harsh? 👇",
    EventType.VAR: "📺 VAR decision — did they get it right? 👇",
    EventType.SCREAMER: "🚀 Rate this strike 1–10 in the comments!",
    EventType.GREAT_SAVE: "🧤 Save of the tournament? 👇",
    EventType.NEAR_MISS: "😱 How on earth did that stay out?! 👇",
    EventType.SKILL: "✨ Filthy skill — who else could pull this off? 👇",
    EventType.HIGHLIGHT: "🔥 Massive moment — your thoughts? 👇",
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


def _score_str(home_score: int | None, away_score: int | None) -> str:
    if home_score is None or away_score is None:
        return ""
    return f"{home_score}-{away_score}"


def build_caption(
    fixture: Fixture, event: MatchEvent,
    home_score: int | None = None, away_score: int | None = None,
) -> str:
    """Title + context + engagement question, reflecting real score/minute."""
    headline = _HEADLINES.get(event.event_type, "Big moment!")
    minute = f"{event.minute}'" if event.minute is not None else ""
    actor = ""
    if event.player and event.team:
        actor = f"{event.player} ({event.team})"
    elif event.player:
        actor = event.player
    elif event.team:
        actor = event.team

    score = _score_str(home_score, away_score)
    score_bit = f" — {fixture.home_team} {score} {fixture.away_team}" if score else ""
    title = " ".join(p for p in (headline, minute, actor) if p).strip()

    round_bit = f" · {fixture.round_name}" if fixture.round_name else ""
    context = f"{fixture.home_team} vs {fixture.away_team}{score_bit}{round_bit}".strip()
    if fixture.league_name:
        context += f" · {fixture.league_name}"

    question = _QUESTIONS.get(event.event_type, "What did you make of that? 👇")
    return "\n".join(p for p in (title, context, "", question) if p is not None)


def build_hashtags(fixture: Fixture, event: MatchEvent) -> list[str]:
    def tagify(name: str) -> str:
        return "#" + "".join(c for c in name if c.isalnum())

    tags: list[str] = list(_BASE_TAGS)
    tags += _EVENT_TAGS.get(event.event_type, [])
    if fixture.home_team:
        tags.append(tagify(fixture.home_team))
    if fixture.away_team:
        tags.append(tagify(fixture.away_team))
    # Scorer / player gets their own tag — strong for discovery.
    if event.player:
        surname = event.player.replace(".", "").split()[-1]
        if len(surname) > 2:
            tags.append(tagify(surname))

    seen: set[str] = set()
    unique: list[str] = []
    for tag in tags:
        low = tag.lower()
        if low not in seen and len(tag) > 1:
            seen.add(low)
            unique.append(tag)
    return unique[:20]
