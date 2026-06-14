"""End-to-end LIVE-PATH test: event -> record -> clip-the-event -> upload.

Real WC live data isn't available on the free plan, so this exercises the
actual live capture code (StreamRecorder + ClipFactory.produce) against a
self-generated, copyright-free live HLS stream:

  1. EVENT   — a goal fires at a wall-clock instant.
  2. STREAM  — a local live HLS source stands in for the broadcast feed.
  3. RECORD  — StreamRecorder keeps a rolling on-disk buffer (the real code).
  4. CLIP    — produce() cuts ONLY the event window [-pre, +post], renders 9:16
               and burns in the event scorebug/details (the real code).
  5. UPLOAD  — YouTubePublisher uploads it (PRIVATE).

It proves the live pipeline mechanics that will run identically on a real feed.

Usage:
    python scripts/test_live_capture.py            # render + upload (private)
    python scripts/test_live_capture.py --no-upload
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, ".")

from wcnet.capture.clipper import ClipFactory, StreamRecorder  # noqa: E402
from wcnet.config import get_settings  # noqa: E402
from wcnet.models import EventType, Fixture, MatchEvent  # noqa: E402
from wcnet.publish.youtube import YouTubePublisher  # noqa: E402
from wcnet.youtube_auth import YouTubeAuth  # noqa: E402


def banner(text: str) -> None:
    print("\n" + "=" * 72 + f"\n{text}\n" + "=" * 72)


def start_local_live_stream(ffmpeg: str, tmp: Path) -> subprocess.Popen:
    """Generate a copyright-free LIVE HLS (test pattern + tone) into tmp."""
    manifest = tmp / "live.m3u8"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list",
        "-hls_segment_filename", str(tmp / "seg_%05d.ts"), str(manifest),
    ]
    proc = subprocess.Popen(cmd, cwd=str(tmp))
    for _ in range(40):
        if manifest.exists():
            break
        time.sleep(0.5)
    return proc


def main() -> int:
    do_upload = "--no-upload" not in sys.argv
    settings = get_settings()
    ffmpeg = settings.ffmpeg_binary
    # "Only the event itself" — a tight window around the trigger.
    settings.capture_pre_seconds = 6
    settings.capture_post_seconds = 5

    tmp = Path(tempfile.mkdtemp(prefix="wcnet_live_"))
    httpd: ThreadingHTTPServer | None = None
    gen: subprocess.Popen | None = None
    recorder: StreamRecorder | None = None

    fixture = Fixture(
        fixture_id=424242, home_team="Argentina", away_team="France",
        kickoff_utc=datetime(2022, 12, 18, 15, 0, tzinfo=timezone.utc),
        status_short="2H", elapsed_minutes=23,
        league_name="World Cup", round_name="Final",
    )
    event = MatchEvent(
        fixture_id=fixture.fixture_id, event_id="live-test-goal",
        event_type=EventType.GOAL, layer="A", minute=23,
        team="Argentina", player="L. Messi",
        description="Live-path test goal",
    )

    try:
        banner("STEP 2 — START LOCAL LIVE STREAM (stand-in broadcast feed)")
        gen = start_local_live_stream(ffmpeg, tmp)
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(tmp))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{port}/live.m3u8"
        print(f"  live HLS: {url}")

        banner("STEP 3 — RECORD rolling buffer (StreamRecorder)")
        recorder = StreamRecorder(fixture, url, settings)
        recorder.start()
        warm = settings.capture_pre_seconds + settings.segment_seconds + 5
        print(f"  buffering ~{warm}s so there is pre-event footage...")
        time.sleep(warm)

        banner("STEP 1 — EVENT FIRES")
        event.detected_at = datetime.now(timezone.utc)
        print(f"  {event.event_type.value.upper()} {event.minute}' "
              f"{event.player} @ {event.detected_at:%H:%M:%S}")

        banner(f"STEP 4 — CLIP ONLY THE EVENT  [-{settings.capture_pre_seconds}s, "
               f"+{settings.capture_post_seconds}s] + scorebug")
        factory = ClipFactory(settings)
        clip = factory.produce(recorder, fixture, event)
        if clip is None:
            print("  produce() returned no clip (no buffered segments).")
            return 1
        size_kb = Path(clip.path).stat().st_size // 1024
        print(f"  clip: {clip.path}  ({clip.duration_seconds:.0f}s, {size_kb} KB)")
        print("  description:")
        for line in clip.caption.splitlines():
            if line:
                print(f"      {line}")
        print(f"  tags: {' '.join(clip.hashtags)}")

        if not do_upload:
            banner("DONE (render-only) [PASS]")
            return 0

        banner("STEP 5 — UPLOAD to YouTube (PRIVATE)")
        settings.youtube_privacy = "private"
        r = YouTubePublisher(settings, auth=YouTubeAuth(settings)).publish(clip)
        if r.ok:
            banner("RESULT — LIVE PIPELINE OK [PASS]")
            print(f"  https://youtu.be/{r.remote_id}  (PRIVATE)")
            return 0
        banner("RESULT — UPLOAD FAILED")
        print(f"  {r.error}")
        return 1
    finally:
        if recorder is not None:
            recorder.stop()
        if gen is not None:
            gen.terminate()
        if httpd is not None:
            httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
