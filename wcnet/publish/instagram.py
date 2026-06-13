"""Stage 5 (c) — Instagram Reels publisher via the Meta Graph API.

Flow:
  1. Upload the local MP4 to R2 → obtain a public URL.
  2. POST /{ig-user-id}/media  (media_type=REELS, video_url=<public url>)  →
     creation_id (a media container).
  3. Poll /{creation_id}?fields=status_code until FINISHED.
  4. POST /{ig-user-id}/media_publish  (creation_id) → published media id.
"""

from __future__ import annotations

import logging
import time

import requests

from ..config import Settings
from ..models import RenderedClip
from ..utils.retry import resilient
from .base import PublishResult, Publisher
from .r2 import R2Uploader

log = logging.getLogger("wcnet.publish.instagram")

_CAPTION_MAX = 2200


class InstagramPublisher(Publisher):
    platform = "instagram"

    def __init__(self, settings: Settings, uploader: R2Uploader | None = None) -> None:
        self._s = settings
        self._r2 = uploader or R2Uploader(settings)
        self._base = f"https://graph.facebook.com/{settings.ig_graph_version}"

    def is_configured(self) -> bool:
        return bool(
            self._s.ig_user_id
            and self._s.ig_access_token
            and self._r2.is_configured()
        )

    @resilient(attempts=3)
    def _create_container(self, video_url: str, caption: str) -> str:
        resp = requests.post(
            f"{self._base}/{self._s.ig_user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "share_to_feed": "true",
                "access_token": self._s.ig_access_token,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if "id" not in data:
            raise RuntimeError(f"IG container error: {data}")
        return data["id"]

    def _wait_ready(self, creation_id: str) -> bool:
        """Poll the container until Meta finishes ingesting the video."""
        for _ in range(30):  # up to ~5 min
            try:
                resp = requests.get(
                    f"{self._base}/{creation_id}",
                    params={
                        "fields": "status_code,status",
                        "access_token": self._s.ig_access_token,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                status = resp.json().get("status_code", "")
                log.debug("IG container %s status=%s", creation_id, status)
                if status == "FINISHED":
                    return True
                if status == "ERROR":
                    return False
            except requests.RequestException:
                pass
            time.sleep(10)
        return False

    @resilient(attempts=3)
    def _publish_container(self, creation_id: str) -> str:
        resp = requests.post(
            f"{self._base}/{self._s.ig_user_id}/media_publish",
            data={
                "creation_id": creation_id,
                "access_token": self._s.ig_access_token,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if "id" not in data:
            raise RuntimeError(f"IG publish error: {data}")
        return data["id"]

    def publish(self, clip: RenderedClip) -> PublishResult:
        try:
            public_url = self._r2.upload(clip.path)
            caption = clip.full_description[:_CAPTION_MAX]
            creation_id = self._create_container(public_url, caption)
            if not self._wait_ready(creation_id):
                return PublishResult(self.platform, False,
                                     error="container not FINISHED")
            media_id = self._publish_container(creation_id)
            log.info("Instagram Reel published: id=%s", media_id)
            return PublishResult(self.platform, True, remote_id=media_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Instagram publish failed")
            return PublishResult(self.platform, False, error=str(exc))
