"""End-to-end test: real YouTube video → 9:16 clip → upload to YOUR channel.

Exercises the full publishing half of the pipeline against the 2022 World Cup
final, using the project's real modules:

  1. SEARCH  — yt-dlp finds the match video on YouTube (no API quota needed).
  2. CLIP    — yt-dlp downloads a short section around the action.
  3. RENDER  — ClipFactory renders it to mobile-native 9:16 H.264/AAC.
  4. UPLOAD  — YouTubePublisher uploads it to your channel (forced PRIVATE).

⚠️  The uploaded clip is set to PRIVATE visibility (visible only to you in
    YouTube Studio) because World Cup footage is copyrighted. Delete it after
    you've confirmed the pipeline works.

Usage:
    python scripts/test_full_youtube.py
    python scripts/test_full_youtube.py "custom search query"
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, ".")

import yt_dlp  # noqa: E402

from wcnet.captioning import build_caption, build_hashtags  # noqa: E402
from wcnet.capture.clipper import ClipFactory  # noqa: E402
from wcnet.config import get_settings  # noqa: E402
from wcnet.models import EventType, Fixture, MatchEvent, RenderedClip  # noqa: E402
from wcnet.publish.youtube import YouTubePublisher  # noqa: E402
from wcnet.youtube_auth import YouTubeAuth  # noqa: E402

DEFAULT_QUERY = "Argentina vs France 2022 World Cup Final extended highlights"
CLIP_LEN = 24.0  # seconds (well under the 60s short-form cap)


def banner(text: str) -> None:
    print("\n" + "=" * 72 + f"\n{text}\n" + "=" * 72)


def find_video(query: str, ffmpeg_dir: str) -> dict:
    """Use yt-dlp's own search (no API key/quota) to pick a good candidate."""
    banner(f"STEP 1/4 — SEARCH youtube: {query!r}")
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch15:{query}", download=False)
    entries = [e for e in (res.get("entries") or []) if e and e.get("id")]
    if not entries:
        raise RuntimeError("No search results")

    def score(e: dict) -> float:
        views = e.get("view_count") or 0
        dur = e.get("duration") or 0
        # Prefer real highlight reels (1–25 min) over Shorts or 3-hour fulls.
        ok_dur = 60 <= dur <= 1500
        return (views * (3.0 if ok_dur else 1.0))

    best = max(entries, key=score)
    print(f"  picked: {best.get('title')!r}")
    print(f"  id={best['id']}  views={best.get('view_count')}  "
          f"duration={best.get('duration')}s  channel={best.get('channel')}")
    return best


def clip_start(duration: float | None) -> float:
    """Pick a start offset that lands inside real footage, not an intro card."""
    if duration and duration > 120:
        return 70.0
    if duration:
        return max(3.0, duration * 0.25)
    return 30.0


def download_video(video_id: str, ffmpeg_dir: str, out_base: Path) -> Path:
    """Download a full progressive file via yt-dlp's NATIVE http downloader.

    We deliberately avoid yt-dlp's ffmpeg-based section download: ffmpeg uses
    its own TLS stack, which the local VPN/proxy MITM breaks. yt-dlp's own
    HTTPS works fine, so we fetch a single progressive file (no remote ffmpeg)
    and trim locally in the render step.
    """
    banner("STEP 2/4 — DOWNLOAD (progressive, native downloader)")
    opts = {
        "quiet": True,
        "no_warnings": True,
        # Single-file progressive first (no ffmpeg needed at all); fall back to
        # a small merged rendition (local ffmpeg merge of local files is fine).
        "format": "b[ext=mp4][acodec!=none][vcodec!=none]/18/"
                  "bv*[height<=480]+ba/best",
        "outtmpl": str(out_base) + ".%(ext)s",
        "merge_output_format": "mp4",
        "ffmpeg_location": ffmpeg_dir,
        "overwrites": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

    candidates = sorted(out_base.parent.glob(out_base.name + ".*"))
    raw = next((c for c in candidates if c.suffix == ".mp4"), None) or candidates[0]
    print(f"  downloaded: {raw}  ({raw.stat().st_size // 1024} KB)")
    return raw


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY
    settings = get_settings()
    ffmpeg_dir = str(Path(settings.ffmpeg_binary).parent)
    # Make ffmpeg + ffprobe discoverable to yt-dlp and any child process.
    os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

    work = settings.clips_dir / "e2e_test"
    work.mkdir(parents=True, exist_ok=True)
    raw_base = work / "raw_segment"
    final_mp4 = work / "final_2022_vertical.mp4"

    # 1 + 2 — find & download
    video = find_video(query, ffmpeg_dir)
    raw = download_video(video["id"], ffmpeg_dir, raw_base)
    start = clip_start(video.get("duration"))

    # 3 — render 9:16 using the project's real renderer (trims [start, +24s])
    banner(f"STEP 3/4 — RENDER 9:16 from [{start:.0f}s … {start + CLIP_LEN:.0f}s]")
    factory = ClipFactory(settings)
    factory._render_vertical(raw, final_mp4, offset=start, duration=CLIP_LEN)
    print(f"  rendered: {final_mp4}  ({final_mp4.stat().st_size // 1024} KB)")

    # build metadata for the 2022 final
    fixture = Fixture(
        fixture_id=999999, home_team="Argentina", away_team="France",
        kickoff_utc=datetime(2022, 12, 18, 15, 0, tzinfo=timezone.utc),
        status_short="FT", elapsed_minutes=120,
        league_name="World Cup", round_name="Final",
    )
    event = MatchEvent(
        fixture_id=fixture.fixture_id, event_id="e2e-test-final",
        event_type=EventType.GOAL, layer="A", minute=108,
        team="Argentina", player="L. Messi",
        description="Highlight from the 2022 World Cup Final (pipeline test clip)",
    )
    clip = RenderedClip(
        path=str(final_mp4), fixture=fixture, event=event,
        duration_seconds=CLIP_LEN,
        caption=build_caption(fixture, event),
        hashtags=build_hashtags(fixture, event),
    )

    if os.environ.get("WCNET_TEST_SKIP_UPLOAD"):
        banner("STOP — skipping upload (WCNET_TEST_SKIP_UPLOAD set). Render OK [PASS]")
        print(f"  9:16 clip ready at: {final_mp4}")
        return 0

    # 4 — upload (forced PRIVATE for this copyrighted test clip)
    banner("STEP 4/4 — UPLOAD to YouTube (PRIVATE)")
    settings.youtube_privacy = "private"
    print("  A browser window will open for one-time Google consent —")
    print("  log in with the account that owns the channel and approve.\n")
    publisher = YouTubePublisher(settings, auth=YouTubeAuth(settings))
    result = publisher.publish(clip)

    if result.ok:
        banner("RESULT — UPLOAD OK [PASS]")
        print(f"  Private video id: {result.remote_id}")
        print(f"  Watch (you only): https://youtu.be/{result.remote_id}")
        print("  It will appear as PRIVATE in YouTube Studio. Delete when done.")
        return 0
    banner("RESULT — UPLOAD FAILED")
    print(f"  error: {result.error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
