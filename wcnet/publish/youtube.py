"""Stage 5 (a) — YouTube Shorts publisher.

Resumable, chunked media upload via the YouTube Data API v3. OAuth tokens are
cached to disk after the first consent so the daemon runs without human input
(and silently refreshed when they expire). ``#Shorts`` is injected into the
description so the upload is classified as a Short.
"""

from __future__ import annotations

import logging

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from ..config import Settings
from ..models import RenderedClip
from ..youtube_auth import YouTubeAuth
from .base import PublishResult, Publisher

log = logging.getLogger("wcnet.publish.youtube")

_TITLE_MAX = 100


class YouTubePublisher(Publisher):
    platform = "youtube"

    def __init__(self, settings: Settings, auth: YouTubeAuth | None = None) -> None:
        self._s = settings
        self._auth = auth or YouTubeAuth(settings)

    def is_configured(self) -> bool:
        return self._auth.is_configured()

    # ── publish ─────────────────────────────────────────────────────────--
    def publish(self, clip: RenderedClip) -> PublishResult:
        try:
            youtube = build("youtube", "v3", credentials=self._auth.credentials(),
                            cache_discovery=False)

            title = clip.caption.splitlines()[0][:_TITLE_MAX]
            description = clip.full_description
            if "#shorts" not in description.lower():
                description = f"{description}\n#Shorts"

            body = {
                "snippet": {
                    "title": title,
                    "description": description,
                    "tags": [t.lstrip("#") for t in clip.hashtags],
                    "categoryId": self._s.youtube_category_id,
                },
                "status": {
                    "privacyStatus": self._s.youtube_privacy,
                    "selfDeclaredMadeForKids": False,
                },
            }

            media = MediaFileUpload(
                clip.path, mimetype="video/mp4", chunksize=4 * 1024 * 1024,
                resumable=True,
            )
            request = youtube.videos().insert(
                part="snippet,status", body=body, media_body=media
            )

            response = None
            retries = 0
            while response is None:
                try:
                    status, response = request.next_chunk()
                    if status:
                        log.debug("YouTube upload %d%%", int(status.progress() * 100))
                except HttpError as exc:
                    if exc.resp.status in (500, 502, 503, 504) and retries < 5:
                        retries += 1
                        log.warning("YouTube chunk retry %d (%s)", retries, exc)
                        continue
                    raise

            video_id = response["id"]
            log.info("YouTube Shorts published: https://youtu.be/%s", video_id)
            return PublishResult(self.platform, True, remote_id=video_id)

        except Exception as exc:  # noqa: BLE001
            log.exception("YouTube publish failed")
            return PublishResult(self.platform, False, error=str(exc))
