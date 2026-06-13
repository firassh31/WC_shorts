"""LIFECYCLE STAGE 3 — dual-layer event detector & match ticker.

A background worker per live match. On each rate-limited poll it pulls the
fixture timeline (and, if configured, a free-text commentary feed) and runs a
two-layer filter:

  • Layer A (Structural): unambiguous data events — Goals, Penalties, Red Cards,
    VAR decisions — straight from the structured timeline.
  • Layer B (Contextual): natural-language phrase matching over commentary text
    — "screamer", "unbelievable save", "hits the post", "what a pass", ...

Each fresh trigger is deduped against the persistent state store and handed to
a callback (the capture pipeline) with the wall-clock instant of detection.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Optional, Protocol

from ..config import Settings
from ..discovery.football_api import FootballAPI
from ..models import EventType, Fixture, MatchEvent
from ..state import StateStore
from ..utils.retry import safe

log = logging.getLogger("wcnet.monitor")

EventCallback = Callable[[MatchEvent], None]


# ── Layer A: structured timeline → EventType ────────────────────────────────
def _classify_structural(item: dict[str, Any]) -> Optional[EventType]:
    etype = (item.get("type") or "").lower()
    detail = (item.get("detail") or "").lower()
    comments = (item.get("comments") or "").lower()

    if etype == "goal":
        if "penalty" in detail:
            return EventType.PENALTY
        if "missed" in detail:  # missed penalty is still highlight-worthy
            return EventType.PENALTY
        return EventType.GOAL
    if etype == "card":
        if "red" in detail:
            return EventType.RED_CARD
        if "yellow" in detail:
            return EventType.YELLOW_CARD
    if etype == "var":
        return EventType.VAR
    if "penalty" in detail or "penalty" in comments:
        return EventType.PENALTY
    return None


# ── Layer B: contextual NL phrase matching ──────────────────────────────────
_CONTEXTUAL_PATTERNS: list[tuple[EventType, re.Pattern[str]]] = [
    (EventType.SCREAMER, re.compile(
        r"\b(screamer|thunderbolt|rocket|worldie|stunner|top corner|"
        r"into the top|unstoppable|from distance|long range)\b", re.I)),
    (EventType.GREAT_SAVE, re.compile(
        r"\b(unbelievable save|incredible save|great save|fingertip|"
        r"point[- ]blank|denied by the keeper|world[- ]class save|"
        r"what a save)\b", re.I)),
    (EventType.NEAR_MISS, re.compile(
        r"\b(hits the (post|bar|crossbar|woodwork)|off the (post|bar)|"
        r"narrowly misses|inches wide|just wide|so close|rattles the)\b", re.I)),
    (EventType.SKILL, re.compile(
        r"\b(what a (pass|run|ball|touch)|outrageous|sublime|magic|"
        r"nutmeg|megs|dribble|beats? (his|the|two|three) man|"
        r"piece of skill|brilliant)\b", re.I)),
    (EventType.HIGHLIGHT, re.compile(
        r"\b(chance|opportunity|dangerous|breakaway|counter[- ]attack|"
        r"goalmouth scramble|controvers)\w*\b", re.I)),
]


def _classify_contextual(text: str) -> Optional[EventType]:
    for event_type, pattern in _CONTEXTUAL_PATTERNS:
        if pattern.search(text):
            return event_type
    return None


class CommentaryProvider(Protocol):
    """Pluggable free-text commentary source for Layer B.

    Return a list of ``(line_id, minute, text)`` tuples. The default ecosystem
    ships without a paid commentary feed, so the structured timeline's own
    ``comments`` field is mined for Layer B as a zero-cost baseline; swap in a
    richer provider (e.g. a live-text-commentary API) by implementing this.
    """

    def fetch(self, fixture: Fixture) -> list[tuple[str, Optional[int], str]]: ...


class MatchMonitor(threading.Thread):
    """Per-match ticker thread running both detection layers."""

    def __init__(
        self,
        fixture: Fixture,
        settings: Settings,
        football: FootballAPI,
        state: StateStore,
        on_event: EventCallback,
        commentary_provider: CommentaryProvider | None = None,
    ) -> None:
        super().__init__(name=f"monitor-{fixture.fixture_id}", daemon=True)
        self._fixture = fixture
        self._s = settings
        self._football = football
        self._state = state
        self._on_event = on_event
        self._commentary = commentary_provider
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # ── one polling pass ────────────────────────────────────────────────--
    @safe(label="monitor.poll")
    def _poll_once(self) -> str:
        """Returns the fixture's current status (so the loop can self-terminate)."""
        events = self._football.fetch_events(self._fixture.fixture_id)
        for idx, item in enumerate(events):
            self._handle_structural(idx, item)
            # Mine the structured comment field for Layer B at zero extra cost.
            comment = item.get("comments") or ""
            if comment:
                self._handle_contextual(f"struct-{idx}", item.get("time", {})
                                        .get("elapsed"), comment)

        if self._commentary is not None:
            for line_id, minute, text in self._commentary.fetch(self._fixture):
                self._handle_contextual(line_id, minute, text)

        # Refresh status to know when the match is over.
        live = self._football.fetch_live_fixtures()
        for fx in live:
            if fx.fixture_id == self._fixture.fixture_id:
                return fx.status_short
        return "FT"  # no longer in the live feed → assume finished

    def _handle_structural(self, idx: int, item: dict[str, Any]) -> None:
        event_type = _classify_structural(item)
        if event_type is None:
            return
        t = item.get("time", {}) or {}
        minute = t.get("elapsed")
        team = (item.get("team") or {}).get("name")
        player = (item.get("player") or {}).get("name")
        # Deterministic id so the same timeline row never re-fires.
        event_id = (f"A:{self._fixture.fixture_id}:{event_type.value}:"
                    f"{minute}:{t.get('extra')}:{player}")
        event = MatchEvent(
            fixture_id=self._fixture.fixture_id,
            event_id=event_id,
            event_type=event_type,
            layer="A",
            minute=minute,
            team=team,
            player=player,
            description=item.get("detail") or item.get("comments") or event_type.value,
        )
        self._dispatch(event)

    def _handle_contextual(
        self, line_id: str, minute: Optional[int], text: str
    ) -> None:
        event_type = _classify_contextual(text)
        if event_type is None:
            return
        event_id = f"B:{self._fixture.fixture_id}:{line_id}"
        event = MatchEvent(
            fixture_id=self._fixture.fixture_id,
            event_id=event_id,
            event_type=event_type,
            layer="B",
            minute=minute,
            team=None,
            player=None,
            description=text.strip()[:240],
        )
        self._dispatch(event)

    def _dispatch(self, event: MatchEvent) -> None:
        """Dedup, then hand off to the capture pipeline."""
        event_hash = event.unique_hash()
        # Atomic claim: only the first observer of this event proceeds.
        if not self._state.register_event(
            event.event_id, event_hash, event.fixture_id, event.event_type.value
        ):
            return
        log.info(
            "⚽ [%s] %s %s' — %s (%s)",
            event.layer, event.event_type.value.upper(),
            event.minute, event.description[:80], self._fixture.title,
        )
        # Capture pipeline runs in its own thread; never block the ticker.
        self._on_event(event)

    # ── thread loop ──────────────────────────────────────────────────────-
    def run(self) -> None:
        log.info("Ticker started for %s", self._fixture.title)
        interval = max(self._s.football_poll_seconds, 15)  # rate-limit floor
        terminal = {"FT", "AET", "PEN", "CANC", "ABD", "AWD", "WO"}
        while not self._stop.is_set():
            status = self._poll_once() or "LIVE"
            if status in terminal:
                log.info("Match %s finished (%s) — stopping ticker",
                         self._fixture.title, status)
                break
            self._stop.wait(interval)
        log.info("Ticker stopped for %s", self._fixture.title)
