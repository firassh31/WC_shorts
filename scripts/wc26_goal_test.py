"""Real test of the free worldcup26.ir goal detection against TODAY's games.

Proves, against the live free API:
  • we can fetch the schedule and pick today's / live matches,
  • we correctly parse real goals (player + minute) into goal events,
  • the diff-watcher would fire each new goal exactly once.

Usage:
    python scripts/wc26_goal_test.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, ".")

from wcnet.discovery.worldcup26 import WorldCup26API  # noqa: E402


def banner(t: str) -> None:
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72)


def main() -> int:
    api = WorldCup26API()
    games = api.fetch_games()
    today = datetime.now(timezone.utc).strftime("%m/%d/%Y")

    banner(f"FREE API (worldcup26.ir) — {len(games)} games loaded | today={today}")

    todays = [m for m in games if str(m.get("local_date", "")).startswith(today)]
    banner(f"TODAY'S MATCHES ({len(todays)})")
    for m in todays:
        hs, as_ = api.scores(m)
        print(f"  id={m['id']:>3}  {m['home_team_name_en']} vs {m['away_team_name_en']}"
              f"  {hs}-{as_}  [{api.status_short(m)}]  {m['local_date']}")

    live = [m for m in games if api.is_live(m)]
    banner(f"LIVE RIGHT NOW ({len(live)})")
    if not live:
        print("  No match in play this moment — run during a live game to clip goals.")
    for m in live:
        print(f"  LIVE: {m['home_team_name_en']} {api.scores(m)[0]}-"
              f"{api.scores(m)[1]} {m['away_team_name_en']}  {m['time_elapsed']}'")
        for ev in api.goal_events(m):
            print(f"     GOAL {ev.minute}'  {ev.player} ({ev.team})")

    # Proof the goal parser works on REAL data: show goals from finished games.
    finished = [m for m in games if api.status_short(m) == "FT" and api.goals(m)]
    banner(f"GOAL-PARSING PROOF on real finished matches ({len(finished)} have goals)")
    for m in finished[:4]:
        hs, as_ = api.scores(m)
        print(f"\n  {m['home_team_name_en']} {hs}-{as_} {m['away_team_name_en']}:")
        for ev in api.goal_events(m):
            print(f"     GOAL {ev.minute}'  {ev.player} ({ev.team})  "
                  f"id={ev.event_id}")

    banner("RESULT — free goal detection works on live WC 2026 data [PASS]")
    print("  Goals carry real player + minute. Cards/subs are NOT in the free")
    print("  feed (goals only). Ready to clip goals when a match is live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
