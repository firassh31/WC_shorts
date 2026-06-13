"""Domain data standards shared across the pipeline (Stage 1, data models).

These immutable-ish dataclasses are the contract passed between the discovery,
monitoring, capture and publishing stages. Keeping them centralized means each
stage agrees on field names and the dedup layer can hash them consistently.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class EventType(str, Enum):
    """Normalized highlight categories driving captions/hashtags."""

    GOAL = "goal"
    PENALTY = "penalty"
    RED_CARD = "red_card"
    YELLOW_CARD = "yellow_card"
    VAR = "var"
    SUBSTITUTION = "substitution"
    # Layer B (contextual / subjective)
    SCREAMER = "screamer"
    GREAT_SAVE = "great_save"
    NEAR_MISS = "near_miss"
    SKILL = "skill"
    HIGHLIGHT = "highlight"  # generic contextual catch-all


# Which event types are worth clipping & publishing.
PUBLISHABLE_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.GOAL,
        EventType.PENALTY,
        EventType.RED_CARD,
        EventType.VAR,
        EventType.SCREAMER,
        EventType.GREAT_SAVE,
        EventType.NEAR_MISS,
        EventType.SKILL,
        EventType.HIGHLIGHT,
    }
)


@dataclass(frozen=True)
class Fixture:
    """A single World Cup match as reported by the football data API."""

    fixture_id: int
    home_team: str
    away_team: str
    kickoff_utc: datetime
    status_short: str  # NS, 1H, HT, 2H, ET, LIVE, FT, ...
    elapsed_minutes: Optional[int]
    league_name: str
    round_name: str

    @property
    def is_live(self) -> bool:
        return self.status_short in {"1H", "2H", "ET", "BT", "P", "LIVE", "HT"}

    @property
    def title(self) -> str:
        return f"{self.home_team} vs {self.away_team}"

    def search_queries(self) -> list[str]:
        """Optimized YouTube queries to find the live broadcast feed."""
        base = f"{self.home_team} vs {self.away_team}"
        return [
            f"{base} live stream world cup",
            f"{base} live world cup {self.kickoff_utc.year}",
            f"{base} en vivo mundial",
            f"{base} full match live",
        ]


@dataclass(frozen=True)
class LiveStream:
    """A validated live YouTube stream for a fixture."""

    video_id: int | str
    channel_title: str
    title: str
    view_count: int
    is_official_ish: bool
    watch_url: str


@dataclass
class MatchEvent:
    """A detected in-match highlight ready to be clipped."""

    fixture_id: int
    event_id: str  # provider id or deterministic synthetic id
    event_type: EventType
    layer: str  # "A" (structural) | "B" (contextual)
    minute: Optional[int]
    team: Optional[str]
    player: Optional[str]
    description: str
    # Wall-clock instant (UTC) at which the event was detected; this is what
    # maps into the rolling capture buffer.
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def unique_hash(self) -> str:
        """Stable content hash used by the duplication guard."""
        raw = "|".join(
            str(p)
            for p in (
                self.fixture_id,
                self.event_type.value,
                self.minute,
                self.team,
                self.player,
                # Description is normalized so near-identical commentary lines
                # collapse to the same hash.
                self.description.strip().lower()[:120],
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class RenderedClip:
    """A finished 9:16 MP4 with all publishing metadata attached."""

    path: str
    fixture: Fixture
    event: MatchEvent
    duration_seconds: float
    caption: str
    hashtags: list[str]

    @property
    def full_description(self) -> str:
        tags = " ".join(self.hashtags)
        return f"{self.caption}\n\n{tags}".strip()
