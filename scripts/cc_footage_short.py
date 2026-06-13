"""Real-footage Short using ONLY Creative-Commons licensed video.

This is the legitimate "real video" path: it searches YouTube restricted to
CC-BY licensed uploads (footage creators have explicitly permitted for reuse),
clips it to a 9:16 Short, and uploads it with the required CC attribution.

  1. SEARCH  — YouTube Data API, videoLicense=creativeCommon (CC-BY only).
  2. CLIP    — yt-dlp downloads the video (native downloader).
  3. RENDER  — ClipFactory renders a 9:16 H.264/AAC Short.
  4. UPLOAD  — YouTubePublisher uploads (UNLISTED) WITH CC attribution.

Usage:
    python scripts/cc_footage_short.py
    python scripts/cc_footage_short.py "Messi goal" 12   # query + start second
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

from wcnet.capture.clipper import ClipFactory  # noqa: E402
from wcnet.config import get_settings  # noqa: E402
from wcnet.discovery.youtube_hunter import YouTubeHunter  # noqa: E402
from wcnet.models import EventType, Fixture, MatchEvent, RenderedClip  # noqa: E402
from wcnet.publish.youtube import YouTubePublisher  # noqa: E402
from wcnet.youtube_auth import YouTubeAuth  # noqa: E402

DEFAULT_QUERY = "football goal"
CLIP_LEN = 22.0


def banner(text: str) -> None:
    print("\n" + "=" * 72 + f"\n{text}\n" + "=" * 72)


def download(video_id: str, ffmpeg_dir: str, out_base: Path) -> Path:
    banner("STEP 2/4 — DOWNLOAD (CC-licensed source, native downloader)")
    opts = {
        "quiet": True, "no_warnings": True,
        "format": "b[ext=mp4][height<=720][acodec!=none][vcodec!=none]/18/"
                  "bv*[height<=720]+ba/best",
        "outtmpl": str(out_base) + ".%(ext)s",
        "merge_output_format": "mp4",
        "ffmpeg_location": ffmpeg_dir,
        "overwrites": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    cands = sorted(out_base.parent.glob(out_base.name + ".*"))
    raw = next((c for c in cands if c.suffix == ".mp4"), None) or cands[0]
    print(f"  downloaded: {raw}  ({raw.stat().st_size // 1024} KB)")
    return raw


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY
    start = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    settings = get_settings()
    ffmpeg_dir = str(Path(settings.ffmpeg_binary).parent)
    os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

    auth = YouTubeAuth(settings)
    hunter = YouTubeHunter(settings, auth=auth)

    banner(f"STEP 1/4 — SEARCH (Creative Commons only): {query!r}")
    clip_src = hunter.find_clip(query, creative_commons=True)
    if clip_src is None:
        print("  No Creative-Commons footage found for that query.")
        print("  CC coverage for big matches is sparse — try a broader query,")
        print("  a smaller league/fan upload, or license footage for big games.")
        return 2
    print(f"  picked: {clip_src.title!r}")
    print(f"  by: {clip_src.channel_title}  views={clip_src.view_count}")
    print(f"  url: {clip_src.watch_url}")

    work = settings.clips_dir / "cc_test"
    work.mkdir(parents=True, exist_ok=True)
    raw = download(clip_src.video_id, ffmpeg_dir, work / "cc_raw")

    banner(f"STEP 3/4 — RENDER 9:16 from [{start:.0f}s … {start + CLIP_LEN:.0f}s]")
    final_mp4 = work / "cc_short_vertical.mp4"
    factory = ClipFactory(settings)
    factory._render_vertical(raw, final_mp4, offset=start, duration=CLIP_LEN)
    print(f"  rendered: {final_mp4}  ({final_mp4.stat().st_size // 1024} KB)")

    # ── REQUIRED CC-BY attribution ──────────────────────────────────────────
    attribution = (
        f"Source: \"{clip_src.title}\" by {clip_src.channel_title}\n"
        f"{clip_src.watch_url}\n"
        f"Licensed under CC BY 3.0 (https://creativecommons.org/licenses/by/3.0/). "
        f"Edited (clipped & reframed to 9:16)."
    )
    hashtags = ["#Shorts", "#football", "#soccer", "#goal", "#highlights", "#fyp"]
    caption = f"{clip_src.title[:80]}\n\n{attribution}"

    fixture = Fixture(
        fixture_id=0, home_team="", away_team="",
        kickoff_utc=datetime.now(timezone.utc), status_short="FT",
        elapsed_minutes=None, league_name="", round_name="",
    )
    event = MatchEvent(
        fixture_id=0, event_id="cc-clip", event_type=EventType.GOAL,
        layer="A", minute=None, team=None, player=None, description="CC clip",
    )
    clip = RenderedClip(
        path=str(final_mp4), fixture=fixture, event=event,
        duration_seconds=CLIP_LEN, caption=caption, hashtags=hashtags,
    )

    if os.environ.get("WCNET_TEST_SKIP_UPLOAD"):
        banner("STOP — skipping upload. Render OK [PASS]")
        print(f"  9:16 clip ready: {final_mp4}")
        return 0

    banner("STEP 4/4 — UPLOAD to YouTube (UNLISTED, with CC attribution)")
    settings.youtube_privacy = "unlisted"
    result = YouTubePublisher(settings, auth=auth).publish(clip)
    if result.ok:
        banner("RESULT — UPLOAD OK [PASS]")
        print(f"  video id: {result.remote_id}")
        print(f"  watch: https://youtu.be/{result.remote_id}")
        print("  Unlisted. Verify the source's CC license is genuine before going public.")
        return 0
    banner("RESULT — UPLOAD FAILED")
    print(f"  error: {result.error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
