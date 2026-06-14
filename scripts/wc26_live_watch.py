"""Free LIVE goal pipeline: worldcup26.ir goals -> stream -> clip -> upload.

Watches a live World Cup 2026 match via the free worldcup26.ir feed and, on each
NEW goal, finds the match's live stream, clips the goal from a rolling buffer,
burns in the scorebug (with running score), and uploads it to YouTube (PRIVATE).

Modes:
  --dry-run            detect goals only — no stream, no recording, no upload
                       (safe to run anytime; with --match it replays a match's
                       goals to prove the wiring).
  --match <id>         target a specific match id (else the first live match).
  --no-upload          record + clip but skip the upload.

Usage:
    python scripts/wc26_live_watch.py --dry-run --match 4     # wiring proof
    python scripts/wc26_live_watch.py                         # full, live match
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, ".")

from wcnet.capture.clipper import ClipFactory, StreamRecorder  # noqa: E402
from wcnet.config import get_settings  # noqa: E402
from wcnet.discovery.worldcup26 import GoalWatcher, WorldCup26API  # noqa: E402
from wcnet.discovery.youtube_hunter import YouTubeHunter  # noqa: E402
from wcnet.publish.youtube import YouTubePublisher  # noqa: E402
from wcnet.youtube_auth import YouTubeAuth  # noqa: E402

POLL_SECONDS = 20


def banner(t: str) -> None:
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72)


def find_match(api: WorldCup26API, games: list[dict], match_id: int | None,
               dry: bool) -> dict | None:
    if match_id is not None:
        return next((m for m in games if api._to_int(m.get("id")) == match_id), None)
    live = [m for m in games if api.is_live(m)]
    return live[0] if live else None


def main() -> int:
    dry = "--dry-run" in sys.argv
    do_upload = "--no-upload" not in sys.argv and not dry
    match_id = None
    if "--match" in sys.argv:
        match_id = int(sys.argv[sys.argv.index("--match") + 1])

    settings = get_settings()
    # Goals are detected on a poll, so allow buildup before the trigger.
    settings.capture_pre_seconds = 35
    settings.capture_post_seconds = 12

    api = WorldCup26API()
    watcher = GoalWatcher(api)
    games = api.fetch_games()
    match = find_match(api, games, match_id, dry)

    if match is None:
        banner("No target match")
        print("  No live match right now. Re-run during a game, or use "
              "--dry-run --match <id> to test the wiring on a finished match.")
        return 1

    fixture = api.to_fixture(match)
    banner(f"TARGET: {fixture.title}  [{api.status_short(match)}]  "
           f"score {api.scores(match)[0]}-{api.scores(match)[1]}")

    # ── DRY RUN: prove detection/clip-plan wiring without a stream ───────────
    if dry:
        print("  DRY-RUN — detecting goals (no stream/record/upload)\n")
        for ev in api.goal_events(match):  # not primed -> reports all once
            hs, as_ = api.scores(match)
            print(f"  GOAL {ev.minute}'  {ev.player} ({ev.team})  "
                  f"-> would clip + scorebug + upload  [{fixture.home_team} "
                  f"{hs}-{as_} {fixture.away_team}]")
        banner("DRY-RUN OK [PASS] — wiring verified on real data")
        return 0

    # ── LIVE: find stream, record rolling buffer, clip each new goal ─────────
    auth = YouTubeAuth(settings)
    hunter = YouTubeHunter(settings, auth=auth)
    banner("FIND LIVE STREAM")
    stream = hunter.find_live_stream(fixture)
    if stream is None:
        print("  No live stream found for this match. Cannot record.")
        return 1
    print(f"  stream: {stream.title!r} ({stream.channel_title})")
    url = hunter.resolve_manifest_url(stream.watch_url)

    recorder = StreamRecorder(fixture, url, settings)
    recorder.start()
    factory = ClipFactory(settings)
    publisher = YouTubePublisher(settings, auth=auth) if do_upload else None
    settings.youtube_privacy = "private"

    watcher.prime(match)  # only goals from NOW on
    banner("WATCHING FOR GOALS (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(POLL_SECONDS)
            games = api.fetch_games()
            m = next((x for x in games
                      if api._to_int(x.get("id")) == fixture.fixture_id), None)
            if m is None:
                continue
            for ev in watcher.new_goals(m):
                ev.detected_at = datetime.now(timezone.utc)
                hs, as_ = api.scores(m)
                print(f"\n⚽ GOAL {ev.minute}' {ev.player} ({ev.team})  "
                      f"{fixture.home_team} {hs}-{as_} {fixture.away_team}")
                clip = factory.produce(recorder, fixture, ev,
                                       home_score=hs, away_score=as_)
                if clip is None:
                    print("   (no buffered footage yet)")
                    continue
                if publisher:
                    r = publisher.publish(clip)
                    print(f"   upload: {'OK https://youtu.be/' + r.remote_id if r.ok else 'FAIL ' + str(r.error)}")
                else:
                    print(f"   clip: {clip.path}")
            if api.status_short(m) == "FT":
                print("\nMatch finished — stopping.")
                break
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        recorder.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
