"""PoC — render ONE copyright-safe generated goal Short (no footage).

Produces a 9:16 animated goal Short from data only, with original synth music.
Nothing from any broadcast is used, so there is zero Content ID exposure.

Usage:
    python scripts/poc_generated_short.py
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, ".")

from wcnet.capture.generator import GoalClipGenerator  # noqa: E402
from wcnet.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    gen = GoalClipGenerator(settings)
    out = settings.clips_dir / "poc" / "goal_short.mp4"

    print("Rendering generated goal Short (data-only, copyright-safe)...")
    gen.render(
        competition="World Cup",
        round_name="Final",
        home_team="Argentina",
        away_team="France",
        home_score=3,
        away_score=3,
        scorer="Messi",
        minute=108,
        scoring_team="Argentina",
        out_path=out,
        duration=6.0,
    )
    size_kb = out.stat().st_size // 1024
    print(f"\nDONE -> {out}  ({size_kb} KB)")
    print("9:16 1080x1920, H.264/AAC, original visuals + synth music. No footage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
