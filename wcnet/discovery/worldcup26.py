"""Free live World Cup 2026 goal source — worldcup26.ir.

A completely free, no-API-key data source (community project) that updates in
real time during the tournament. It exposes scores and a scorers string that
includes the scoring player AND minute, e.g.  {"J. Quiñones 9'","R. Jiménez 67'"}.

This provider:
  • fetches all 104 games (GET /get/games -> {"games":[...]}),
  • selects today's / live matches,
  • parses goals (player + minute) into MatchEvent objects,
  • diffs successive polls so each NEW goal fires exactly once.

Limitations (free data): GOALS ONLY — no cards, subs, VAR, or contextual events.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from ..models import EventType, Fixture, MatchEvent
from ..utils.http import make_session
from ..utils.retry import resilient

log = logging.getLogger("wcnet.discovery.worldcup26")

BASE_URL = "https://worldcup26.ir"
# Windows' bundled curl uses SChannel (the OS TLS stack), which negotiates with
# this host even when Python's OpenSSL handshake is dropped by the network.
_SYS_CURL = r"C:\Windows\System32\curl.exe"
_NULLISH = {"", "null", "none", "notstarted"}
# Matches "Name 23'" or "Name 90+2'" inside the scorers blob.
_SCORER_RE = re.compile(r"([^\",{}]+?)\s+(\d{1,3})(?:\+\d+)?'")


@dataclass(frozen=True)
class Goal:
    team: str
    player: str
    minute: int


class WorldCup26API:
    """Thin, resilient client over the free worldcup26.ir endpoints."""

    def __init__(self) -> None:
        self._session = make_session()
        self._session.headers.update({"Accept": "application/json"})

    @resilient(attempts=4)
    def fetch_games(self) -> list[dict]:
        try:
            resp = self._session.get(f"{BASE_URL}/get/games", timeout=20)
            resp.raise_for_status()
            return resp.json().get("games", [])
        except requests.exceptions.SSLError:
            # Python's OpenSSL handshake to this host is dropped on some
            # networks; the OS-native TLS stack (Windows SChannel via curl)
            # negotiates fine. Fall back to it.
            return self._curl_get_games()

    def _curl_get_games(self) -> list[dict]:
        curl = _SYS_CURL if os.path.exists(_SYS_CURL) else shutil.which("curl")
        if not curl:
            raise RuntimeError("TLS fallback unavailable: system curl not found")
        out = subprocess.run(
            [curl, "-s", "--max-time", "20", "-H", "Accept: application/json",
             f"{BASE_URL}/get/games"],
            capture_output=True, text=True,
        )
        if out.returncode != 0 or not out.stdout.strip():
            raise RuntimeError(f"curl TLS fallback failed (rc={out.returncode})")
        return json.loads(out.stdout).get("games", [])

    # ── helpers ──────────────────────────────────────────────────────────--
    @staticmethod
    def _to_int(value, default=0):
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def parse_scorers(blob: str | None) -> list[tuple[str, int]]:
        """Extract (player, minute) pairs from a scorers string."""
        if not blob or blob.strip().lower() in _NULLISH:
            return []
        text = blob.replace("“", '"').replace("”", '"').replace("’", "'")
        out: list[tuple[str, int]] = []
        for m in _SCORER_RE.finditer(text):
            name = m.group(1).strip(" \",{}").strip()
            if name and name.lower() not in _NULLISH:
                out.append((name, int(m.group(2))))
        return out

    def is_live(self, match: dict) -> bool:
        finished = str(match.get("finished", "")).upper() == "TRUE"
        elapsed = str(match.get("time_elapsed", "")).strip().lower()
        return (not finished) and (elapsed not in _NULLISH)

    def status_short(self, match: dict) -> str:
        if str(match.get("finished", "")).upper() == "TRUE":
            return "FT"
        return "LIVE" if self.is_live(match) else "NS"

    def scores(self, match: dict) -> tuple[int, int]:
        return self._to_int(match.get("home_score")), self._to_int(match.get("away_score"))

    def to_fixture(self, match: dict) -> Fixture:
        try:
            kickoff = datetime.strptime(
                match.get("local_date", ""), "%m/%d/%Y %H:%M"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            kickoff = datetime.now(timezone.utc)
        elapsed = match.get("time_elapsed", "")
        round_name = f"{match.get('type', 'group').title()} · MD {match.get('matchday', '')}"
        return Fixture(
            fixture_id=self._to_int(match.get("id")),
            home_team=match.get("home_team_name_en", "Home"),
            away_team=match.get("away_team_name_en", "Away"),
            kickoff_utc=kickoff,
            status_short=self.status_short(match),
            elapsed_minutes=self._to_int(elapsed, None) if str(elapsed).isdigit() else None,
            league_name="World Cup",
            round_name=round_name.strip(" ·"),
        )

    def goals(self, match: dict) -> list[Goal]:
        home = match.get("home_team_name_en", "Home")
        away = match.get("away_team_name_en", "Away")
        result = [Goal(home, p, m) for p, m in self.parse_scorers(match.get("home_scorers"))]
        result += [Goal(away, p, m) for p, m in self.parse_scorers(match.get("away_scorers"))]
        return result

    def goal_events(self, match: dict) -> list[MatchEvent]:
        """All goals in a match as MatchEvent objects (GOAL, player, minute)."""
        fid = self._to_int(match.get("id"))
        events = []
        for g in self.goals(match):
            events.append(MatchEvent(
                fixture_id=fid,
                event_id=f"WC26:{fid}:{g.team}:{g.player}:{g.minute}",
                event_type=EventType.GOAL, layer="A", minute=g.minute,
                team=g.team, player=g.player,
                description=f"Goal — {g.player} {g.minute}'",
            ))
        return events


class GoalWatcher:
    """Diffs successive polls of one match so each new goal fires once."""

    def __init__(self, api: WorldCup26API) -> None:
        self._api = api
        self._seen: dict[int, set[str]] = {}

    def prime(self, match: dict) -> None:
        """Record current goals as already-seen (so only future goals fire)."""
        fid = self._api._to_int(match.get("id"))
        self._seen[fid] = {e.event_id for e in self._api.goal_events(match)}

    def new_goals(self, match: dict) -> list[MatchEvent]:
        fid = self._api._to_int(match.get("id"))
        seen = self._seen.setdefault(fid, set())
        fresh = []
        for ev in self._api.goal_events(match):
            if ev.event_id not in seen:
                seen.add(ev.event_id)
                fresh.append(ev)
        return fresh
