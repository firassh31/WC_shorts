"""Free live runner — worldcup26.ir goals → record → clip → LOCAL .mp4.

Used for live World Cup 2026 matches on the free data feed (the paid
API-Football plan doesn't cover the 2026 season). Watches one match, and on
each NEW goal records the discovered live stream, cuts the goal, burns in the
simple event-text overlay, and writes the finished clip to data/clips/.

Resilience:
  • every poll is wrapped — a transient API/network error never crashes the run;
  • a stream watchdog detects a dead feed (no fresh segments) and automatically
    fails over to a different live stream.

No uploading — the pipeline stops at local files.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .capture.clipper import ClipFactory, StreamRecorder
from .config import Settings, get_settings
from .discovery.worldcup26 import GoalWatcher, WorldCup26API
from .discovery.youtube_hunter import YouTubeHunter
from .logging_setup import configure_logging
from .youtube_auth import YouTubeAuth

log = logging.getLogger("wcnet.live_runner")

POLL_SECONDS = 20
STALE_SECONDS = 50          # no fresh segment for this long → stream is dead
RESTART_GRACE = 35          # don't health-check a stream within this of (re)start


def _safe_fetch_match(api: WorldCup26API, fixture_id: int) -> dict | None:
    """Fetch the target match; swallow transient errors (never crash the loop)."""
    try:
        games = api.fetch_games()
    except Exception as exc:  # noqa: BLE001
        log.warning("worldcup26 poll failed (transient): %s", exc)
        return None
    return next((g for g in games if api._to_int(g.get("id")) == fixture_id), None)


def _start_recording(hunter: YouTubeHunter, fixture, settings: Settings,
                     failed: set[str]) -> tuple[StreamRecorder | None, str | None]:
    """Find a live stream (excluding failed ones) and start recording it."""
    try:
        stream = hunter.find_live_stream(fixture, exclude=failed)
    except Exception as exc:  # noqa: BLE001
        log.warning("stream search failed (transient): %s", exc)
        return None, None
    if stream is None:
        return None, None
    log.info("Stream: %r (%s)", stream.title, stream.channel_title)
    recorder = StreamRecorder(fixture, stream.watch_url, settings)
    recorder.start()
    return recorder, str(stream.video_id)


def run_wc26_live(match_id: int | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.data_dir / "wcnet.log")
    # Goals are detected on a poll, so allow generous buildup before the trigger.
    settings.capture_pre_seconds = 35
    settings.capture_post_seconds = 12

    api = WorldCup26API()
    watcher = GoalWatcher(api)

    try:
        games = api.fetch_games()
    except Exception as exc:  # noqa: BLE001
        log.error("Could not reach worldcup26.ir: %s — try again shortly.", exc)
        return 1

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
    factory = ClipFactory(settings)
    failed: set[str] = set()

    recorder, current_vid = _start_recording(hunter, fixture, settings, failed)
    if recorder is None:
        log.error("No live stream found for %s — cannot record.", fixture.title)
        return 1
    started_at = time.time()
    watcher.prime(match)  # only goals from now on

    log.info("👀 Watching for goals (Ctrl+C to stop)...")
    try:
        while True:
            time.sleep(POLL_SECONDS)

            # 1. Poll goals (resiliently).
            m = _safe_fetch_match(api, fixture.fixture_id)
            if m is not None:
                for ev in watcher.new_goals(m):
                    ev.detected_at = datetime.now(timezone.utc)
                    hs, as_ = api.scores(m)
                    log.info("⚽ GOAL %s' %s (%s)  %s %d-%d %s",
                             ev.minute, ev.player, ev.team,
                             fixture.home_team, hs, as_, fixture.away_team)
                    if recorder is None:
                        log.warning("   no active stream — cannot clip this goal")
                        continue
                    clip = factory.produce(recorder, fixture, ev,
                                           home_score=hs, away_score=as_)
                    if clip is None:
                        log.warning("   no buffered footage for this goal")
                    else:
                        log.info("   📁 saved %s", clip.path)

            # 2. Stream watchdog → fail over to a different feed if dead.
            if recorder is not None and time.time() - started_at > RESTART_GRACE:
                age = recorder.seconds_since_last_segment()
                if age is None or age > STALE_SECONDS:
                    log.warning("Stream stale (%s) — switching feed.",
                                "no segments" if age is None else f"{age:.0f}s old")
                    if current_vid:
                        failed.add(current_vid)
                    recorder.stop()
                    recorder, current_vid = _start_recording(
                        hunter, fixture, settings, failed)
                    started_at = time.time()
                    if recorder is None:
                        log.warning("No alternate stream yet; retrying next cycle.")

            # 3. Stop when the match ends.
            if m is not None and api.status_short(m) == "FT":
                log.info("Match finished — stopping.")
                break
    except KeyboardInterrupt:
        log.info("Interrupted.")
    finally:
        if recorder is not None:
            recorder.stop()
    return 0
