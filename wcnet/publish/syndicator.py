"""Stage 5 (d) — concurrent syndication across all three networks.

Fans the finished clip out to YouTube, TikTok and Instagram in parallel via a
thread pool. Each platform is independently deduped against the state store and
isolated so one failure never blocks the others.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import Settings
from ..models import RenderedClip
from ..state import StateStore
from ..youtube_auth import YouTubeAuth
from .base import PublishResult, Publisher
from .instagram import InstagramPublisher
from .r2 import R2Uploader
from .tiktok import TikTokPublisher
from .youtube import YouTubePublisher

log = logging.getLogger("wcnet.publish.syndicator")


class Syndicator:
    def __init__(
        self, settings: Settings, state: StateStore,
        youtube_auth: YouTubeAuth | None = None,
    ) -> None:
        self._s = settings
        self._state = state
        r2 = R2Uploader(settings)
        all_publishers: list[Publisher] = [
            YouTubePublisher(settings, auth=youtube_auth),
            TikTokPublisher(settings),
            InstagramPublisher(settings, uploader=r2),
        ]
        self._publishers = [p for p in all_publishers if p.is_configured()]
        skipped = {p.platform for p in all_publishers} - {
            p.platform for p in self._publishers
        }
        if skipped:
            log.warning("Skipping unconfigured platforms: %s", ", ".join(skipped))
        log.info("Syndicator active for: %s",
                 ", ".join(p.platform for p in self._publishers) or "<none>")

    def syndicate(self, clip: RenderedClip) -> list[PublishResult]:
        event_hash = clip.event.unique_hash()
        targets = [
            p for p in self._publishers
            if not self._state.already_published(event_hash, p.platform)
        ]
        if not targets:
            log.info("Clip already syndicated everywhere; nothing to do.")
            return []

        results: list[PublishResult] = []
        with ThreadPoolExecutor(max_workers=len(targets),
                                thread_name_prefix="pub") as pool:
            future_map = {pool.submit(p.publish, clip): p for p in targets}
            for future in as_completed(future_map):
                publisher = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    result = PublishResult(publisher.platform, False, error=str(exc))
                results.append(result)
                self._state.record_publication(
                    event_hash, result.platform, result.remote_id,
                    "ok" if result.ok else "error",
                )

        ok = [r.platform for r in results if r.ok]
        bad = [f"{r.platform}({r.error})" for r in results if not r.ok]
        log.info("Syndication done. ok=%s failed=%s", ok or "-", bad or "-")
        return results
