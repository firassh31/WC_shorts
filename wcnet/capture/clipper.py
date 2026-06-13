"""LIFECYCLE STAGE 4 — rolling stream buffer, instant clip extraction, 9:16 render.

Strategy
--------
A single, cheap FFmpeg process per stream continuously demuxes the live HLS
feed into short ``.ts`` segments using ``-c copy`` (no re-encode → near-zero
CPU). Segment filenames are timestamped, giving us an on-disk DVR window. A
janitor thread prunes segments older than the configured buffer.

When an event fires at wall-clock instant ``T`` we:
  1. wait until ``T + post`` has elapsed (so the trailing segments exist),
  2. select the segments overlapping ``[T - pre, T + post]``,
  3. concat-copy them into one clip,
  4. trim to the exact window and render to mobile-native 9:16 H.264/AAC.

Only the final render re-encodes — and only ~30 s of footage — so the whole
pipeline stays light even under a 24/7 schedule.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..models import Fixture, MatchEvent, RenderedClip
from ..captioning import build_caption, build_hashtags
from ..utils.retry import resilient
from .overlay import scorebug_for_event

log = logging.getLogger("wcnet.capture")

_SEG_RE = re.compile(r"seg_(\d{8}_\d{6})_(\d+)\.ts$")


class StreamRecorder:
    """Continuously buffers a live stream into a timestamped segment ring."""

    def __init__(self, fixture: Fixture, manifest_url: str, settings: Settings) -> None:
        self._fixture = fixture
        self._url = manifest_url
        self._s = settings
        self._dir = settings.buffer_dir / str(fixture.fixture_id)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._proc: subprocess.Popen[bytes] | None = None
        self._stop = threading.Event()
        self._janitor: threading.Thread | None = None
        self._recorder: threading.Thread | None = None

    @property
    def buffer_dir(self) -> Path:
        return self._dir

    # ── lifecycle ───────────────────────────────────────────────────────--
    def start(self) -> None:
        self._recorder = threading.Thread(
            target=self._record_loop, name=f"rec-{self._fixture.fixture_id}",
            daemon=True,
        )
        self._janitor = threading.Thread(
            target=self._janitor_loop, name=f"jan-{self._fixture.fixture_id}",
            daemon=True,
        )
        self._recorder.start()
        self._janitor.start()
        log.info("Recorder started for %s → %s", self._fixture.title, self._dir)

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        # Best-effort cleanup of the buffer directory.
        shutil.rmtree(self._dir, ignore_errors=True)
        log.info("Recorder stopped for %s", self._fixture.title)

    # ── recorder loop (auto-reconnect on stream drop) ──────────────────────
    def _record_loop(self) -> None:
        pattern = str(self._dir / "seg_%Y%m%d_%H%M%S_%03d.ts")
        while not self._stop.is_set():
            cmd = [
                self._s.ffmpeg_binary,
                "-hide_banner", "-loglevel", "error",
                "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-i", self._url,
                "-c", "copy",
                "-f", "segment",
                "-segment_time", str(self._s.segment_seconds),
                "-strftime", "1",
                "-reset_timestamps", "1",
                "-segment_format", "mpegts",
                pattern,
            ]
            try:
                log.debug("Spawning recorder ffmpeg: %s", " ".join(cmd))
                self._proc = subprocess.Popen(cmd)
                self._proc.wait()
            except Exception:  # noqa: BLE001
                log.exception("Recorder ffmpeg crashed; will reconnect")
            if self._stop.is_set():
                break
            # Brief stream drop → backoff then reconnect (resiliency mandate).
            log.warning("Stream dropped for %s; reconnecting in 3s",
                        self._fixture.title)
            self._stop.wait(3)

    # ── janitor: prune old segments ────────────────────────────────────────
    def _janitor_loop(self) -> None:
        while not self._stop.wait(self._s.segment_seconds * 2):
            cutoff = time.time() - self._s.buffer_window_seconds
            for seg in self._dir.glob("seg_*.ts"):
                try:
                    if seg.stat().st_mtime < cutoff:
                        seg.unlink(missing_ok=True)
                except OSError:
                    pass

    # ── segment selection helpers ─────────────────────────────────────────-
    @staticmethod
    def _seg_start(path: Path) -> float | None:
        m = _SEG_RE.search(path.name)
        if not m:
            return None
        dt = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").replace(
            tzinfo=timezone.utc
        )
        return dt.timestamp()

    def select_segments(self, window_start: float, window_end: float) -> list[Path]:
        """All buffered segments overlapping the requested wall-clock window."""
        seg_len = self._s.segment_seconds
        chosen: list[tuple[float, Path]] = []
        for seg in self._dir.glob("seg_*.ts"):
            start = self._seg_start(seg)
            if start is None:
                continue
            end = start + seg_len
            if end >= window_start and start <= window_end:
                chosen.append((start, seg))
        chosen.sort(key=lambda x: x[0])
        return [p for _, p in chosen]


class ClipFactory:
    """Turns a fired event + buffered segments into a finished 9:16 MP4."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings

    # ── public entry point ────────────────────────────────────────────────
    def produce(
        self, recorder: StreamRecorder, fixture: Fixture, event: MatchEvent
    ) -> RenderedClip | None:
        trigger = event.detected_at.timestamp()
        pre = self._s.capture_pre_seconds
        post = self._s.capture_post_seconds
        window_start = trigger - pre
        window_end = trigger + post

        # 1. Wait for the trailing footage to be safely written to disk.
        self._wait_for(window_end + self._s.segment_seconds + 1)

        # 2. Collect the overlapping segments.
        segments = recorder.select_segments(window_start, window_end)
        if not segments:
            log.warning("No buffered segments for event %s; skipping",
                        event.event_id)
            return None

        work_dir = self._s.clips_dir / str(fixture.fixture_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        stamp = event.detected_at.strftime("%Y%m%d_%H%M%S")
        concat_ts = work_dir / f"concat_{stamp}.ts"
        out_mp4 = work_dir / f"{event.event_type.value}_{stamp}.mp4"
        scorebug = work_dir / f"bug_{stamp}.png"

        try:
            # 3. Concat (copy, no re-encode) the raw segments.
            self._concat_copy(segments, concat_ts)
            # 4. Compute precise trim offset relative to the first segment.
            first_start = recorder._seg_start(segments[0]) or window_start
            offset = max(0.0, window_start - first_start)
            duration = min(float(pre + post), 59.0)  # hard < 60s guarantee
            # 5. Build the event-driven scorebug, then trim + render to 9:16.
            scorebug_for_event(scorebug, fixture, event)
            self._render_vertical(concat_ts, out_mp4, offset, duration,
                                  scorebug_png=scorebug)
        finally:
            concat_ts.unlink(missing_ok=True)
            scorebug.unlink(missing_ok=True)

        caption = build_caption(fixture, event)
        hashtags = build_hashtags(fixture, event)
        log.info("Rendered clip → %s (%.1fs)", out_mp4, duration)
        return RenderedClip(
            path=str(out_mp4),
            fixture=fixture,
            event=event,
            duration_seconds=duration,
            caption=caption,
            hashtags=hashtags,
        )

    def _wait_for(self, until_ts: float) -> None:
        remaining = until_ts - time.time()
        if remaining > 0:
            time.sleep(remaining)

    @resilient(attempts=3, exceptions=(subprocess.SubprocessError, OSError))
    def _concat_copy(self, segments: list[Path], out_path: Path) -> None:
        list_file = out_path.with_suffix(".txt")
        list_file.write_text(
            "".join(f"file '{seg.as_posix()}'\n" for seg in segments),
            encoding="utf-8",
        )
        cmd = [
            self._s.ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(out_path),
        ]
        self._run(cmd)
        list_file.unlink(missing_ok=True)

    def _vertical_core(self) -> str:
        """The base 9:16 (1080x1920) chain, producing a labelled pad [vv]."""
        if self._s.render_mode == "center_crop":
            return (
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,setsar=1[vv]"
            )
        # Default: minimalist blurred top/bottom padding around a fitted feed.
        return (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=22:6,eq=brightness=-0.06[bgb];"
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[vv]"
        )

    @resilient(attempts=3, exceptions=(subprocess.SubprocessError, OSError))
    def _render_vertical(
        self, src: Path, out_path: Path, offset: float, duration: float,
        scorebug_png: Path | None = None, apply_filter: bool = True,
    ) -> None:
        """Trim + render to 9:16, optionally grading + compositing a scorebug.

        ``apply_filter`` adds a basic colour grade; ``scorebug_png`` (if given)
        is overlaid as a banner at the top of the frame.
        """
        graph = self._vertical_core()
        last = "vv"
        if apply_filter:
            graph += f";[{last}]eq=contrast=1.06:saturation=1.12,vignette=PI/5[gr]"
            last = "gr"

        cmd = [
            self._s.ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{offset:.3f}", "-i", str(src),
        ]
        if scorebug_png is not None:
            cmd += ["-i", str(scorebug_png)]
            graph += f";[{last}][1:v]overlay=(W-w)/2:70[outv]"
        else:
            graph += f";[{last}]copy[outv]"

        cmd += [
            "-t", f"{duration:.3f}",
            "-filter_complex", graph,
            "-map", "[outv]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-r", "30", "-g", "60",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-movflags", "+faststart",
            str(out_path),
        ]
        self._run(cmd)

    @staticmethod
    def _run(cmd: list[str]) -> None:
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise subprocess.SubprocessError(
                f"ffmpeg failed ({proc.returncode}): "
                f"{proc.stderr.decode(errors='ignore')[-500:]}"
            )
