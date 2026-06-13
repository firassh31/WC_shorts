"""Shared YouTube OAuth credential manager.

A single source of truth for YouTube credentials used by *both* the live-stream
hunter (Search) and the Shorts publisher (Upload). Because the provided Google
project ships only an OAuth *client* (no standalone API key), all YouTube calls
authenticate via OAuth — one browser consent on first run, then a cached,
auto-refreshed token keeps the daemon fully autonomous.
"""

from __future__ import annotations

import logging
import threading

import google.auth.transport.requests
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import Settings

log = logging.getLogger("wcnet.youtube_auth")

# readonly → search.list / videos.list ; upload → videos.insert (Shorts).
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
]


class YouTubeAuth:
    """Thread-safe, disk-cached OAuth credentials for the YouTube Data API."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._lock = threading.Lock()
        self._creds: Credentials | None = None

    def is_configured(self) -> bool:
        return self._s.youtube_client_secrets.exists()

    def credentials(self) -> Credentials:
        """Return valid credentials, performing/refreshing auth as needed."""
        with self._lock:
            if self._creds and self._creds.valid:
                return self._creds

            creds: Credentials | None = None
            cache = self._s.youtube_token_cache
            if cache.exists():
                creds = Credentials.from_authorized_user_file(str(cache), SCOPES)

            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(google.auth.transport.requests.Request())
                    self._persist(creds)
                except RefreshError:
                    log.warning("YouTube token refresh failed; re-authorizing")
                    creds = None

            if not creds or not creds.valid:
                # One-time interactive consent (first run only). The Desktop
                # client's loopback redirect lets run_local_server pick a port.
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._s.youtube_client_secrets), SCOPES
                )
                creds = flow.run_local_server(port=0, prompt="consent")
                self._persist(creds)

            self._creds = creds
            return creds

    def _persist(self, creds: Credentials) -> None:
        cache = self._s.youtube_token_cache
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(creds.to_json(), encoding="utf-8")
