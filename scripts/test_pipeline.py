"""Data-layer smoke test against real World Cup data (free-plan friendly).

Validates the parts of the pipeline the football API feeds:
  1. Discovery   — fetch the season's fixtures and parse them into Fixture objs.
  2. Events      — pull a finished match's real timeline.
  3. Detection   — run the dual-layer classifier (Layer A structural +
                   Layer B contextual) over those real events and print exactly
                   which highlights WOULD be clipped & published.

It does NOT touch YouTube/FFmpeg (a 2022 match has no live stream). It only
proves the data + detection layers work against your API key.

Usage:
    python scripts/test_pipeline.py            # auto-pick recent finished games
    python scripts/test_pipeline.py 12345      # inspect a specific fixture id

Budget: ~3 API calls (well under the free 100/day cap).
"""

from __future__ import annotations

import sys

# Windows consoles default to cp1252 and choke on non-Latin glyphs; force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, ".")

from wcnet.config import get_settings  # noqa: E402
from wcnet.discovery.football_api import FootballAPI  # noqa: E402
from wcnet.models import PUBLISHABLE_EVENTS, MatchEvent  # noqa: E402
from wcnet.monitor.event_detector import (  # noqa: E402
    _classify_contextual,
    _classify_structural,
)


def _print_header(text: str) -> None:
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


def classify_match(api: FootballAPI, fixture, fixture_obj) -> int:
    """Run both detection layers over a match's real timeline."""
    events = api.fetch_events(fixture_obj.fixture_id)
    _print_header(
        f"{fixture_obj.title}  ({fixture_obj.round_name})  "
        f"— {len(events)} raw timeline events"
    )
    triggered = 0
    seen_hashes: set[str] = set()
    for idx, item in enumerate(events):
        t = (item.get("time", {}) or {}).get("elapsed")
        team = (item.get("team") or {}).get("name")
        player = (item.get("player") or {}).get("name")
        comment = item.get("comments") or item.get("detail") or ""

        # Layer A — structural
        a_type = _classify_structural(item)
        # Layer B — contextual (mining the comment text)
        b_type = _classify_contextual(comment) if comment else None

        for layer, etype, text in (("A", a_type, comment), ("B", b_type, comment)):
            if etype is None:
                continue
            ev = MatchEvent(
                fixture_id=fixture_obj.fixture_id,
                event_id=f"{layer}:{idx}",
                event_type=etype,
                layer=layer,
                minute=t,
                team=team,
                player=player,
                description=text or etype.value,
            )
            h = ev.unique_hash()
            dup = " (DUPLICATE-guarded)" if h in seen_hashes else ""
            seen_hashes.add(h)
            publish = "[WOULD PUBLISH]" if etype in PUBLISHABLE_EVENTS else "-"
            triggered += 1
            print(
                f"  [{layer}] {t!s:>4}'  {etype.value:<11} "
                f"{(player or team or ''):<22} {publish}{dup}"
            )
            if text and layer == "B":
                print(f"        -> matched commentary: \"{text[:70]}\"")
    if triggered == 0:
        print("  (no highlight events classified for this match)")
    return triggered


def main() -> int:
    settings = get_settings()
    api = FootballAPI(settings)

    _print_header(
        f"DISCOVERY — World Cup (league {settings.football_league_id}) "
        f"season {settings.football_season}"
    )
    payload = api._get(
        "fixtures",
        {"league": settings.football_league_id, "season": settings.football_season},
    )
    if payload.get("errors"):
        print("API errors:", payload["errors"])
        return 1

    fixtures = [FootballAPI._parse_fixture(i) for i in payload.get("response", [])]
    print(f"Parsed {len(fixtures)} fixtures.")
    if not fixtures:
        print("No fixtures returned — check plan/season access.")
        return 1

    # Choose targets: explicit id from argv, else the last finished matches
    # (knockout stage → guaranteed goals/cards to exercise the classifier).
    if len(sys.argv) > 1:
        wanted = {int(sys.argv[1])}
        targets = [f for f in fixtures if f.fixture_id in wanted]
    else:
        finished = [f for f in fixtures if f.status_short in {"FT", "AET", "PEN"}]
        targets = sorted(finished, key=lambda f: f.kickoff_utc)[-2:]

    total = 0
    for fx in targets:
        total += classify_match(api, fx, fx)

    _print_header(
        f"RESULT - {total} highlight event(s) detected across "
        f"{len(targets)} match(es). Data + detection layers OK [PASS]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
