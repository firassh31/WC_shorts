"""WCNET entrypoint.

Usage:
    python main.py            # run the autonomous 24/7 network
    python main.py doctor     # validate config, credentials & ffmpeg
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from wcnet.config import get_settings
from wcnet.logging_setup import configure_logging, get_logger
from wcnet.orchestrator import Orchestrator

log = get_logger("wcnet.main")


def _doctor() -> int:
    """Preflight: confirm the host is ready before going live."""
    settings = get_settings()
    configure_logging(settings.log_level)
    ok = True

    ffmpeg = shutil.which(settings.ffmpeg_binary)
    if ffmpeg:
        ver = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True)
        log.info("✔ ffmpeg: %s", ver.stdout.splitlines()[0])
    else:
        log.error("✗ ffmpeg not found on PATH (%s)", settings.ffmpeg_binary)
        ok = False

    checks = {
        "Football API key": bool(settings.football_api_key
                                 and settings.football_api_key != "replace_me"),
        "YouTube OAuth client (client_secrets.json)":
            settings.youtube_client_secrets.exists(),
        "YouTube token cached (autonomous)": settings.youtube_token_cache.exists(),
        "TikTok creds": bool(settings.tiktok_client_key
                             and settings.tiktok_client_secret
                             and settings.tiktok_refresh_token
                             and settings.tiktok_refresh_token != "replace_me"),
        "Instagram creds": bool(settings.ig_user_id and settings.ig_access_token
                                and settings.ig_access_token != "replace_me"),
        "R2 storage": bool(settings.r2_endpoint and settings.r2_public_base_url),
    }
    for label, present in checks.items():
        log.info("%s %s", "✔" if present else "✗", label)
        # Football data + the YouTube OAuth client are hard requirements; the
        # rest gate only their own platform. (YouTube auth uses OAuth, so no
        # standalone API key is needed.)
        if label.startswith(("Football", "YouTube OAuth")) and not present:
            ok = False

    log.info("Clip window: %ds pre + %ds post = %ds (must be < 60)",
             settings.capture_pre_seconds, settings.capture_post_seconds,
             settings.clip_total_seconds)
    log.info("Doctor: %s", "READY ✅" if ok else "NOT READY ❌")
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        return _doctor()
    Orchestrator().run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
