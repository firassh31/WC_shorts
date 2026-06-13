"""Batch: produce ONE scorebugged 9:16 clip per BIG event in a match.

For a given match it pulls the real event timeline (API-Football), keeps only
the big moments (goals, penalties, red cards, VAR, contextual highlights),
tracks the running score, and renders a separate clip per event — each with its
own event-styled scorebug (GOAL/PENALTY/RED CARD ... + score + minute).

Footage source: a local video you supply (e.g. a clip you have the rights to,
or the live buffer in production). Timestamp→event alignment is exact in the
LIVE pipeline (each event clips from the rolling buffer); for a static VOD reel
the offsets are spread across the file, so labels are illustrative.

Usage:
    python scripts/match_to_clips.py <source_video> [fixture_id] [--max N] [--upload]
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, ".")

from wcnet.capture.clipper import ClipFactory  # noqa: E402
from wcnet.capture.overlay import scorebug_for_event  # noqa: E402
from wcnet.config import get_settings  # noqa: E402
from wcnet.discovery.football_api import FootballAPI  # noqa: E402
from wcnet.models import (  # noqa: E402
    PUBLISHABLE_EVENTS, EventType, Fixture, MatchEvent, RenderedClip,
)
from wcnet.monitor.event_detector import _classify_structural  # noqa: E402

CLIP_LEN = 12.0


def banner(text: str) -> None:
    print("\n" + "=" * 72 + f"\n{text}\n" + "=" * 72)


def probe_duration(ffmpeg_bin: str, path: Path) -> float:
    ffprobe = str(Path(ffmpeg_bin).with_name("ffprobe.exe"))
    if not Path(ffprobe).exists():
        ffprobe = "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True,
        )
        return float(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return 120.0


def find_final(api: FootballAPI, settings) -> Fixture | None:
    payload = api._get("fixtures", {"league": settings.football_league_id,
                                    "season": settings.football_season})
    fixtures = [FootballAPI._parse_fixture(i) for i in payload.get("response", [])]
    for fx in fixtures:
        if fx.round_name.strip().lower() == "final":
            return fx
    return fixtures[-1] if fixtures else None


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/match_to_clips.py <source_video> "
              "[fixture_id] [--max N] [--upload]")
        return 1
    source = Path(sys.argv[1])
    if not source.exists():
        print(f"Source video not found: {source}")
        return 1
    do_upload = "--upload" in sys.argv
    max_clips = 6
    skip_idx: set[int] = set()
    if "--max" in sys.argv:
        mi = sys.argv.index("--max")
        max_clips = int(sys.argv[mi + 1])
        skip_idx.add(mi + 1)
    fixture_id = next((int(a) for i, a in enumerate(sys.argv)
                       if i >= 2 and i not in skip_idx and a.isdigit()), None)

    settings = get_settings()
    ffmpeg_dir = str(Path(settings.ffmpeg_binary).parent)
    os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
    api = FootballAPI(settings)

    banner("STEP 1 — RESOLVE MATCH + EVENTS")
    if fixture_id:
        payload = api._get("fixtures", {"id": fixture_id})
        fixture = FootballAPI._parse_fixture(payload["response"][0])
    else:
        fixture = find_final(api, settings)
    if fixture is None:
        print("Could not resolve a fixture.")
        return 1
    print(f"  match: {fixture.title}  ({fixture.round_name})")

    events = api.fetch_events(fixture.fixture_id)

    def sort_key(it):
        t = it.get("time", {}) or {}
        return ((t.get("elapsed") or 0), (t.get("extra") or 0))

    events.sort(key=sort_key)

    # Walk the timeline, track running score, collect the big events.
    hs = as_ = 0
    collected: list[tuple[dict, EventType, int, int]] = []
    for it in events:
        etype = (it.get("type") or "").lower()
        detail = (it.get("detail") or "").lower()
        team = (it.get("team") or {}).get("name", "")
        is_goal = (etype == "goal" and not any(
            w in detail for w in ("missed", "cancelled", "disallowed")))
        if is_goal:
            if team == fixture.home_team:
                hs += 1
            elif team == fixture.away_team:
                as_ += 1
        cls = _classify_structural(it)
        if cls in PUBLISHABLE_EVENTS:
            collected.append((it, cls, hs, as_))

    collected = collected[:max_clips]
    print(f"  big events to clip: {len(collected)}")
    if not collected:
        print("  none found.")
        return 1

    dur = probe_duration(settings.ffmpeg_binary, source)
    span_lo, span_hi = 4.0, max(5.0, dur - CLIP_LEN - 1)
    factory = ClipFactory(settings)
    out_dir = settings.clips_dir / "match_events"
    out_dir.mkdir(parents=True, exist_ok=True)

    banner(f"STEP 2 — RENDER {len(collected)} PER-EVENT CLIPS (9:16 + scorebug)")
    produced: list[tuple[Path, MatchEvent, int, int]] = []
    for i, (it, cls, h, a) in enumerate(collected):
        t = (it.get("time", {}) or {})
        minute = t.get("elapsed")
        player = (it.get("player") or {}).get("name")
        team = (it.get("team") or {}).get("name")
        event = MatchEvent(
            fixture_id=fixture.fixture_id, event_id=f"m2c-{i}",
            event_type=cls, layer="A", minute=minute, team=team,
            player=player, description=it.get("detail") or cls.value,
        )
        offset = span_lo + (span_hi - span_lo) * (i / max(1, len(collected) - 1))
        bug = out_dir / f"bug_{i}.png"
        out_mp4 = out_dir / f"{i:02d}_{cls.value}_{minute}.mp4"
        scorebug_for_event(bug, fixture, event, home_score=h, away_score=a)
        factory._render_vertical(source, out_mp4, offset=offset,
                                 duration=CLIP_LEN, scorebug_png=bug,
                                 apply_filter=True)
        bug.unlink(missing_ok=True)
        produced.append((out_mp4, event, h, a))
        print(f"  [{i+1}/{len(collected)}] {cls.value:<9} {minute}'  "
              f"{(player or team or ''):<20} {h}-{a}  -> {out_mp4.name}")

    banner(f"DONE — {len(produced)} per-event clips in {out_dir}")
    if not do_upload:
        print("  (render-only; pass --upload to publish each to YouTube)")
        return 0

    # Optional upload of each clip.
    from wcnet.captioning import build_caption, build_hashtags
    from wcnet.publish.youtube import YouTubePublisher
    from wcnet.youtube_auth import YouTubeAuth
    settings.youtube_privacy = "private"
    pub = YouTubePublisher(settings, auth=YouTubeAuth(settings))
    for path, event, h, a in produced:
        clip = RenderedClip(
            path=str(path), fixture=fixture, event=event,
            duration_seconds=CLIP_LEN,
            caption=build_caption(fixture, event),
            hashtags=build_hashtags(fixture, event),
        )
        r = pub.publish(clip)
        print(f"  upload {event.event_type.value} {event.minute}': "
              f"{'OK ' + (r.remote_id or '') if r.ok else 'FAIL ' + str(r.error)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
