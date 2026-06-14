"""LIFECYCLE STAGE 2 (a) — Match discovery via API-Football v3.

Polls the World Cup schedule and selects matches that are live now (or about
to kick off). Supports both the direct ``api-sports.io`` host and the RapidAPI
gateway transparently — only the auth headers differ.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from ..config import Settings
from ..models import Fixture
from ..utils.retry import resilient

log = logging.getLogger("wcnet.discovery.football")

# Statuses API-Football reports for an in-progress match.
_LIVE_STATUSES = {"1H", "2H", "ET", "BT", "P", "HT", "LIVE", "INT"}
# Pre-match window we treat as "about to start" (minutes).
_UPCOMING_WINDOW_MIN = 20


class FootballAPI:
    """Thin, resilient client over the API-Football v3 fixtures endpoints."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._session = requests.Session()
        self._base = f"https://{settings.football_api_host}"
        if settings.football_api_via_rapidapi:
            self._session.headers.update(
                {
                    "x-rapidapi-key": settings.football_api_key,
                    "x-rapidapi-host": settings.football_api_host,
                }
            )
        else:
            self._session.headers.update(
                {"x-apisports-key": settings.football_api_key}
            )

    @resilient(attempts=5)
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}/{path.lstrip('/')}"
        resp = self._session.get(url, params=params, timeout=20)
        if resp.status_code == 429:
            # Rate limited — raise a retryable error so backoff kicks in.
            raise requests.exceptions.ConnectionError("API-Football rate limited (429)")
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            log.warning("API-Football returned errors: %s", payload["errors"])
        return payload

    # ── parsing ───────────────────────────────────────────────────────────
    @staticmethod
    def _parse_fixture(item: dict[str, Any]) -> Fixture:
        fx = item["fixture"]
        league = item["league"]
        teams = item["teams"]
        kickoff = datetime.fromtimestamp(fx["timestamp"], tz=timezone.utc)
        return Fixture(
            fixture_id=int(fx["id"]),
            home_team=teams["home"]["name"],
            away_team=teams["away"]["name"],
            kickoff_utc=kickoff,
            status_short=fx["status"]["short"],
            elapsed_minutes=fx["status"].get("elapsed"),
            league_name=league.get("name", "World Cup"),
            round_name=league.get("round", ""),
        )

    # ── public API ──────────────────────────────────────────────────────--
    def fetch_today_fixtures(self) -> list[Fixture]:
        """All World Cup fixtures scheduled for the current UTC day."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        payload = self._get(
            "fixtures",
            {
                "league": self._s.football_league_id,
                "season": self._s.football_season,
                "date": today,
            },
        )
        fixtures = [self._parse_fixture(i) for i in payload.get("response", [])]
        log.info("Discovered %d World Cup fixtures for %s", len(fixtures), today)
        return fixtures

    def fetch_live_fixtures(self) -> list[Fixture]:
        """Fixtures the API currently reports as in-progress."""
        payload = self._get(
            "fixtures",
            {
                "league": self._s.football_league_id,
                "season": self._s.football_season,
                "live": "all",
            },
        )
        return [self._parse_fixture(i) for i in payload.get("response", [])]

    def select_actionable(self) -> list[Fixture]:
        """Automated selector: matches that are live or imminently kicking off.

        We union the dedicated ``live=all`` feed (most authoritative) with any
        of today's fixtures whose kickoff is within the upcoming window, so the
        stream hunter can be primed *before* the whistle.
        """
        actionable: dict[int, Fixture] = {}
        now = datetime.now(timezone.utc)

        for fx in self.fetch_live_fixtures():
            if fx.status_short in _LIVE_STATUSES:
                actionable[fx.fixture_id] = fx

        for fx in self.fetch_today_fixtures():
            if fx.fixture_id in actionable:
                continue
            if fx.status_short in _LIVE_STATUSES:
                actionable[fx.fixture_id] = fx
                continue
            mins_to_kick = (fx.kickoff_utc - now).total_seconds() / 60.0
            if 0 <= mins_to_kick <= _UPCOMING_WINDOW_MIN:
                actionable[fx.fixture_id] = fx

        selected = list(actionable.values())
        log.info("Selector flagged %d actionable fixture(s)", len(selected))
        return selected

    @resilient(attempts=4)
    def fetch_fixture(self, fixture_id: int) -> Fixture | None:
        """Fetch a single fixture by id (used for forced --fixture runs)."""
        payload = self._get("fixtures", {"id": fixture_id})
        resp = payload.get("response", [])
        return self._parse_fixture(resp[0]) if resp else None

    @resilient(attempts=4)
    def fetch_events(self, fixture_id: int) -> list[dict[str, Any]]:
        """Raw timeline events (goals, cards, subs, VAR) for a fixture."""
        payload = self._get("fixtures/events", {"fixture": fixture_id})
        return payload.get("response", [])
