"""WCNET entrypoint — renders local highlight clips (no publishing).

Usage:
    python main.py                       # daily discovery (API-Football)
    python main.py --fixture 1380123     # force one API-Football fixture id
    python main.py --wc26 10             # free worldcup26 live runner, match id 10
    python main.py --wc26                # free runner, auto-pick a live match
    python main.py doctor                # validate config, credentials & ffmpeg

Output: finished .mp4 files (+ a .txt with a suggested title/description) under
data/clips/<fixture_id>/, named with the match and event type.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from wcnet.config import get_settings
from wcnet.live_runner import run_wc26_live
from wcnet.logging_setup import configure_logging, get_logger
from wcnet.orchestrator import Orchestrator

log = get_logger("wcnet.main")


def _doctor() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    ok = True

    ffmpeg = shutil.which(settings.ffmpeg_binary) or (
        settings.ffmpeg_binary if shutil.os.path.exists(settings.ffmpeg_binary) else None
    )
    if ffmpeg:
        ver = subprocess.run([settings.ffmpeg_binary, "-version"],
                             capture_output=True, text=True)
        log.info("✔ ffmpeg: %s", ver.stdout.splitlines()[0] if ver.stdout else "ok")
    else:
        log.error("✗ ffmpeg not found (%s)", settings.ffmpeg_binary)
        ok = False

    checks = {
        "Football API key (API-Football path)": bool(
            settings.football_api_key and settings.football_api_key != "replace_me"),
        "YouTube OAuth client (for live stream search)":
            settings.youtube_client_secrets.exists(),
    }
    for label, present in checks.items():
        log.info("%s %s", "✔" if present else "✗", label)

    log.info("Clip window: %ds pre + %ds post (must be < 60)",
             settings.capture_pre_seconds, settings.capture_post_seconds)
    log.info("Output dir: %s", settings.clips_dir)
    log.info("Publishing: DISABLED (local files only)")
    log.info("Doctor: %s", "READY ✅" if ok else "NOT READY ❌")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="WCNET — local highlight clipper")
    parser.add_argument("mode", nargs="?", default="run", choices=["run", "doctor"])
    parser.add_argument("--fixture", type=int, default=None,
                        help="force a single API-Football fixture id (bypass discovery)")
    parser.add_argument("--wc26", nargs="?", type=int, const=-1, default=None,
                        help="free worldcup26 live runner; optional match id "
                             "(omit to auto-pick a live match)")
    parser.add_argument("--stream-url", default=None,
                        help="record this exact stream URL (bypass auto-search); "
                             "use the FIFA-approved feed you've verified")
    args = parser.parse_args()

    if args.mode == "doctor":
        return _doctor()

    if args.wc26 is not None:
        match_id = None if args.wc26 == -1 else args.wc26
        return run_wc26_live(match_id, stream_url=args.stream_url)

    log.info("Discovery via API-Football (needs a paid plan for the 2026 season).")
    log.info("For the FREE WorldCup-2026 feed, use:  python main.py --wc26  "
             "(auto-picks the live/upcoming match)")
    Orchestrator().run_forever(forced_fixture=args.fixture)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
