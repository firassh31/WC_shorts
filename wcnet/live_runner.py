"""Free live runner — worldcup26.ir goals → record → clip → LOCAL .mp4.

Used for live World Cup 2026 matches on the free data feed (the paid
API-Football plan doesn't cover the 2026 season). Watches one match, and on
each NEW goal records the discovered live stream, cuts the goal, burns in the
simple event-text overlay, and writes the finished clip to data/clips/.

No uploading — the pipeline stops at local files.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .capture.clipper import ClipFactory, StreamRecorder
from .config import get_settings
from .discovery.worldcup26 import GoalWatcher, WorldCup26API
from .discovery.youtube_hunter import YouTubeHunter
from .logging_setup import configure_logging
from .youtube_auth import YouTubeAuth

log = logging.getLogger("wcnet.live_runner")

POLL_SECONDS = 20


def run_wc26_live(match_id: int | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.data_dir / "wcnet.log")
    # Goals are detected on a poll, so allow generous buildup before the trigger.
    settings.capture_pre_seconds = 35
    settings.capture_post_seconds = 12

    api = WorldCup26API()
    watcher = GoalWatcher(api)
    games = api.fetch_games()

    if match_id is not None:
        match = next((m for m in games if api._to_int(m.get("id")) == match_id), None)
    else:
        live = [m for m in games if api.is_live(m)]
        match = live[0] if live else None

    if match is None:
        log.error("No target match. Pass a live match id, or run during a game. "
                  "List today's ids:  python scripts/wc26_goal_test.py")
        return 1

    fixture = api.to_fixture(match)
    log.info("🎯 %s [%s]  score %s-%s", fixture.title, api.status_short(match),
             *api.scores(match))

    hunter = YouTubeHunter(settings, auth=YouTubeAuth(settings))
    stream = hunter.find_live_stream(fixture)
    if stream is None:
        log.error("No live stream found for %s — cannot record.", fixture.title)
        return 1
    log.info("Stream: %r (%s)", stream.title, stream.channel_title)
    url = hunter.resolve_manifest_url(stream.watch_url)

    recorder = StreamRecorder(fixture, url, settings)
    recorder.start()
    factory = ClipFactory(settings)
    watcher.prime(match)  # only goals from now on

    log.info("👀 Watching for goals (Ctrl+C to stop)...")
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
                log.info("⚽ GOAL %s' %s (%s)  %s %d-%d %s",
                         ev.minute, ev.player, ev.team,
                         fixture.home_team, hs, as_, fixture.away_team)
                clip = factory.produce(recorder, fixture, ev,
                                       home_score=hs, away_score=as_)
                if clip is None:
                    log.warning("   no buffered footage yet for this goal")
                else:
                    log.info("   📁 saved %s", clip.path)
            if api.status_short(m) == "FT":
                log.info("Match finished — stopping.")
                break
    except KeyboardInterrupt:
        log.info("Interrupted.")
    finally:
        recorder.stop()
    return 0
