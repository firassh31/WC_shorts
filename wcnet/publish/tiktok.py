"""Stage 5 (b) — TikTok publisher via the Content Posting API.

Flow (FILE_UPLOAD direct post):
  1. Refresh the user access token from the cached long-lived refresh token.
  2. POST /v2/post/publish/video/init/  → publish_id + upload_url.
  3. PUT the local MP4 bytes to upload_url with a Content-Range header.
  4. Poll /v2/post/publish/status/fetch/ until the post is published.

Token handling is fully cached so no human input is required after the
one-time OAuth consent that produced the refresh token.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import requests

from ..config import Settings
from ..models import RenderedClip
from ..utils.retry import resilient
from .base import PublishResult, Publisher

log = logging.getLogger("wcnet.publish.tiktok")

_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
_TITLE_MAX = 2200


class TikTokPublisher(Publisher):
    platform = "tiktok"

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def is_configured(self) -> bool:
        return bool(
            self._s.tiktok_client_key
            and self._s.tiktok_client_secret
            and self._s.tiktok_refresh_token
        )

    # ── token management ───────────────────────────────────────────────────
    def _cached_token(self) -> dict[str, Any] | None:
        cache = self._s.tiktok_token_cache
        if cache.exists():
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
        return None

    @resilient(attempts=4)
    def _refresh_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._access_token and now < self._expires_at - 60:
                return self._access_token

            cached = self._cached_token()
            refresh_token = (
                cached.get("refresh_token") if cached else None
            ) or self._s.tiktok_refresh_token

            resp = requests.post(
                _TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "client_key": self._s.tiktok_client_key,
                    "client_secret": self._s.tiktok_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            if "access_token" not in data:
                raise RuntimeError(f"TikTok token error: {data}")

            self._access_token = data["access_token"]
            self._expires_at = now + int(data.get("expires_in", 86400))
            # Persist the (possibly rotated) refresh token for next time.
            self._s.tiktok_token_cache.parent.mkdir(parents=True, exist_ok=True)
            self._s.tiktok_token_cache.write_text(
                json.dumps(data), encoding="utf-8"
            )
            return self._access_token

    # ── publish ─────────────────────────────────────────────────────────--
    @resilient(attempts=3)
    def _init_upload(self, token: str, clip: RenderedClip, size: int) -> dict[str, Any]:
        caption = clip.full_description[:_TITLE_MAX]
        body = {
            "post_info": {
                "title": caption,
                "privacy_level": self._s.tiktok_privacy_level,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
                "video_cover_timestamp_ms": 1000,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,        # single-chunk upload for short clips
                "total_chunk_count": 1,
            },
        }
        resp = requests.post(
            _INIT_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            data=json.dumps(body),
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error", {}).get("code") not in (None, "ok"):
            raise RuntimeError(f"TikTok init error: {payload['error']}")
        return payload["data"]

    @resilient(attempts=3)
    def _upload_bytes(self, upload_url: str, path: str, size: int) -> None:
        with open(path, "rb") as fh:
            data = fh.read()
        headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(size),
            "Content-Range": f"bytes 0-{size - 1}/{size}",
        }
        resp = requests.put(upload_url, headers=headers, data=data, timeout=120)
        resp.raise_for_status()

    def _poll_status(self, token: str, publish_id: str) -> str:
        for _ in range(20):
            try:
                resp = requests.post(
                    _STATUS_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=UTF-8",
                    },
                    data=json.dumps({"publish_id": publish_id}),
                    timeout=20,
                )
                resp.raise_for_status()
                status = resp.json().get("data", {}).get("status", "")
                log.debug("TikTok status: %s", status)
                if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                    return status
                if status == "FAILED":
                    return "FAILED"
            except requests.RequestException:
                pass
            time.sleep(5)
        return "PENDING"

    def publish(self, clip: RenderedClip) -> PublishResult:
        try:
            size = os.path.getsize(clip.path)
            token = self._refresh_token()
            data = self._init_upload(token, clip, size)
            publish_id = data["publish_id"]
            self._upload_bytes(data["upload_url"], clip.path, size)
            status = self._poll_status(token, publish_id)
            ok = status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "PENDING")
            log.info("TikTok publish %s (id=%s)", status, publish_id)
            return PublishResult(self.platform, ok, remote_id=publish_id,
                                 error=None if ok else status)
        except Exception as exc:  # noqa: BLE001
            log.exception("TikTok publish failed")
            return PublishResult(self.platform, False, error=str(exc))
