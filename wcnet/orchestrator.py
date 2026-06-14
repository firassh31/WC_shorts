"""The conductor — wires every lifecycle stage into one 24/7 autonomous loop.

Discovery loop (main thread)
    └─ for each newly-actionable fixture:
         ├─ hunt + resolve its live YouTube stream
         ├─ start a StreamRecorder (rolling buffer)               [Stage 4]
         └─ start a MatchMonitor ticker                            [Stage 3]
                └─ on each detected event → ClipFactory.produce    [Stage 4]
                       └─ save the finished .mp4 to data/clips/    [local only]

Publishing has been removed: the pipeline stops at rendering local clips. An
executor caps how many clips render at once so bursts never overwhelm the host.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .capture.clipper import ClipFactory, StreamRecorder
from .config import Settings, get_settings
from .discovery.football_api import FootballAPI
from .discovery.youtube_hunter import YouTubeHunter
from .logging_setup import configure_logging
from .models import PUBLISHABLE_EVENTS, Fixture, MatchEvent
from .monitor.event_detector import MatchMonitor
from .state import StateStore
from .utils.retry import safe
from .youtube_auth import YouTubeAuth

log = logging.getLogger("wcnet.orchestrator")


class ActiveMatch:
    """Bundle of the per-match workers so we can tear them down cleanly."""

    def __init__(self, fixture: Fixture, recorder: StreamRecorder,
                 monitor: MatchMonitor) -> None:
        self.fixture = fixture
        self.recorder = recorder
        self.monitor = monitor


class Orchestrator:
    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._state = StateStore(self._s.state_db_path)
        self._football = FootballAPI(self._s)
        # One shared OAuth credential manager for both Search and Upload.
        self._yt_auth = YouTubeAuth(self._s)
        self._hunter = YouTubeHunter(self._s, auth=self._yt_auth)
        self._clip_factory = ClipFactory(self._s)
        # Caps concurrent render jobs across all matches.
        self._jobs = ThreadPoolExecutor(max_workers=4, thread_name_prefix="clip")
        self._active: dict[int, ActiveMatch] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ── event → clip → syndicate ───────────────────────────────────────────
    def _on_event(self, fixture: Fixture, event: MatchEvent) -> None:
        """Callback handed to each MatchMonitor; offloads heavy work to pool."""
        recorder = self._active.get(fixture.fixture_id)
        if recorder is None:
            return
        self._jobs.submit(self._process_event, recorder.recorder, fixture, event)

    @safe(label="orchestrator.process_event")
    def _process_event(self, recorder: StreamRecorder, fixture: Fixture,
                        event: MatchEvent) -> None:
        # Only big moments become their own clip — goals, penalties, red cards,
        # VAR, and the contextual highlights. Yellow cards / subs are skipped.
        if event.event_type not in PUBLISHABLE_EVENTS:
            return
        clip = self._clip_factory.produce(recorder, fixture, event)
        if clip is None:
            return
        log.info("📁 Clip ready: %s", clip.path)

    # ── spinning up a match ────────────────────────────────────────────────
    @safe(label="orchestrator.activate")
    def _activate(self, fixture: Fixture) -> None:
        with self._lock:
            if fixture.fixture_id in self._active:
                return

        stream = self._hunter.find_live_stream(fixture)
        if stream is None:
            # Don't mark processed — a stream may appear on a later poll.
            log.info("No live stream yet for %s; will retry next cycle",
                     fixture.title)
            return

        # The recorder uses yt-dlp to pull the watch URL robustly.
        recorder = StreamRecorder(fixture, stream.watch_url, self._s)
        recorder.start()

        monitor = MatchMonitor(
            fixture=fixture,
            settings=self._s,
            football=self._football,
            state=self._state,
            on_event=lambda ev, fx=fixture: self._on_event(fx, ev),
        )
        monitor.start()

        with self._lock:
            self._active[fixture.fixture_id] = ActiveMatch(fixture, recorder, monitor)
        self._state.mark_fixture(fixture.fixture_id, fixture.title,
                                 str(stream.video_id))
        log.info("✅ Activated %s on stream %s", fixture.title, stream.video_id)

    # ── reaping finished matches ───────────────────────────────────────────
    def _reap_finished(self) -> None:
        with self._lock:
            done = [fid for fid, m in self._active.items()
                    if not m.monitor.is_alive()]
            for fid in done:
                match = self._active.pop(fid)
                match.recorder.stop()
                log.info("🧹 Reaped finished match %s", match.fixture.title)

    # ── main discovery loop ────────────────────────────────────────────────
    @safe(label="orchestrator.discovery_cycle")
    def _discovery_cycle(self) -> None:
        self._reap_finished()
        for fixture in self._football.select_actionable():
            with self._lock:
                already_active = fixture.fixture_id in self._active
            if already_active:
                continue
            self._activate(fixture)

    def run_forever(self, forced_fixture: int | None = None) -> None:
        configure_logging(self._s.log_level, self._s.data_dir / "wcnet.log")
        log.info("🚀 WCNET online — env=%s league=%s season=%s",
                 self._s.env, self._s.football_league_id, self._s.football_season)
        interval = max(self._s.football_poll_seconds, 30)
        try:
            if forced_fixture is not None:
                self._run_single_fixture(forced_fixture, interval)
            else:
                while not self._stop.is_set():
                    self._discovery_cycle()
                    self._stop.wait(interval)
        except KeyboardInterrupt:
            log.info("Interrupt received — shutting down")
        finally:
            self.shutdown()

    @safe(label="orchestrator.run_single")
    def _run_single_fixture(self, fixture_id: int, interval: int) -> None:
        """Bypass discovery: activate one fixture and run until it finishes."""
        fixture = self._football.fetch_fixture(fixture_id)
        if fixture is None:
            log.error("Fixture %s not found (or not on your API plan).", fixture_id)
            return
        log.info("🎯 Forced fixture %s — %s [%s]",
                 fixture_id, fixture.title, fixture.status_short)
        self._activate(fixture)
        while not self._stop.is_set():
            self._reap_finished()
            with self._lock:
                if fixture_id not in self._active:
                    log.info("Forced fixture finished — exiting.")
                    break
            self._stop.wait(interval)

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for match in self._active.values():
                match.monitor.stop()
                match.recorder.stop()
            self._active.clear()
        self._jobs.shutdown(wait=False, cancel_futures=True)
        self._state.close()
        log.info("WCNET stopped.")
