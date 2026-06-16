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


def _kickoff_dt(api: WorldCup26API, match: dict):
    from datetime import datetime
    try:
        return datetime.strptime(match.get("local_date", ""), "%m/%d/%Y %H:%M")
    except ValueError:
        return datetime.max


def _select_match(api: WorldCup26API, games: list[dict]):
    """Auto-pick a match: any LIVE one, else the soonest UPCOMING one."""
    live = [m for m in games if api.is_live(m)]
    if live:
        return live[0], "live"
    upcoming = [m for m in games if api.status_short(m) == "NS"]
    upcoming.sort(key=lambda m: _kickoff_dt(api, m))
    return (upcoming[0], "upcoming") if upcoming else (None, None)


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


def run_wc26_live(match_id: int | None = None,
                  stream_url: str | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.data_dir / "wcnet.log")
    # Goals are detected on a poll, so allow generous buildup before the trigger.
    settings.capture_pre_seconds = 35
    settings.capture_post_seconds = 12

    api = WorldCup26API()
    watcher = GoalWatcher(api)

    # Be patient on startup: the host (and/or a local VPN/proxy) can drop TLS
    # intermittently, so retry for a few minutes before giving up.
    games = None
    for attempt in range(1, 7):
        try:
            games = api.fetch_games()
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("worldcup26.ir unreachable (try %d/6): %s", attempt, exc)
            time.sleep(25)
    if games is None:
        log.error(
            "worldcup26.ir still unreachable. This is a network/TLS problem on "
            "this machine — TLS handshakes are being reset (often an unstable "
            "VPN/proxy). Check your connection/VPN and try again.")
        return 1

    if match_id is not None:
        match = next((m for m in games if api._to_int(m.get("id")) == match_id), None)
        kind = "live" if match and api.is_live(match) else "selected"
    else:
        match, kind = _select_match(api, games)

    if match is None:
        log.error("No live or upcoming World Cup match found right now.")
        return 1

    fixture = api.to_fixture(match)
    target_id = fixture.fixture_id
    log.info("🎯 Auto-selected (%s): %s  [%s]  %s",
             kind, fixture.title, match.get("local_date", ""),
             "score %s-%s" % api.scores(match))

    # If it hasn't kicked off yet, wait for it to go live before recording.
    while not api.is_live(match) and api.status_short(match) != "FT":
        log.info("⏳ Waiting for kickoff (%s)... checking again in 60s.",
                 match.get("local_date", ""))
        time.sleep(60)
        m = _safe_fetch_match(api, target_id)
        if m is not None:
            match = m
    if api.status_short(match) == "FT":
        log.info("Match already finished — nothing to record.")
        return 0
    fixture = api.to_fixture(match)
    log.info("🟢 Kickoff — %s is LIVE. Starting capture.", fixture.title)

    factory = ClipFactory(settings)
    failed: set[str] = set()
    hunter = YouTubeHunter(settings, auth=YouTubeAuth(settings))

    if stream_url:
        # User-supplied feed (e.g. the FIFA-approved stream you verified).
        log.info("Using provided stream URL (auto-search bypassed): %s", stream_url)
        recorder = StreamRecorder(fixture, stream_url, settings)
        recorder.start()
        current_vid = None
    else:
        recorder, current_vid = _start_recording(hunter, fixture, settings, failed)
        if recorder is None:
            log.error("No verified match stream for %s — cannot record. "
                      "Pass --stream-url <youtube url> to record a specific feed.",
                      fixture.title)
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

            # 2. Stream watchdog. With a user-supplied URL we just let the
            #    recorder auto-reconnect; in auto mode we fail over to another feed.
            if (stream_url is None and recorder is not None
                    and time.time() - started_at > RESTART_GRACE):
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
