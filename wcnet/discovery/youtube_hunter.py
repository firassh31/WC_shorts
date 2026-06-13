"""LIFECYCLE STAGE 2 (b) — YouTube live-stream hunting & resolution.

Given a live fixture, builds optimized search queries from the two countries,
queries the YouTube Data API v3 Search endpoint filtered to *active* live
streams, scores the candidates with a validation heuristic (view count +
"official-ness"), and resolves the winner's playable HLS manifest URL via
yt-dlp so FFmpeg can ingest it.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
import yt_dlp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..config import Settings
from ..models import Fixture, LiveStream
from ..utils.retry import resilient
from ..youtube_auth import YouTubeAuth

log = logging.getLogger("wcnet.discovery.youtube")

# Signals that a channel is an official broadcaster rather than a re-streamer.
_OFFICIAL_SIGNALS = (
    "fifa", "official", "espn", "fox sports", "bein", "sky sports",
    "bbc", "itv", "telemundo", "tnt sports", "dazn", "optus",
)
# Signals that strongly suggest a junk / scam re-stream we should down-rank.
_NEGATIVE_SIGNALS = ("free", "watch now", "click", "link in", "pes", "fifa 2", "efootball")


class YouTubeHunter:
    """Finds and resolves the best live stream feed for a fixture."""

    def __init__(self, settings: Settings, auth: YouTubeAuth | None = None) -> None:
        self._s = settings
        self._auth = auth or YouTubeAuth(settings)
        self._yt_client = None  # lazily built with OAuth credentials

    @property
    def _yt(self):
        """Lazily build (and reuse) an OAuth-authenticated YouTube client.

        Using OAuth credentials for Search as well as Upload means the single
        provided Google OAuth client covers the whole pipeline — no separate
        API key required. google-auth refreshes the access token in place.
        """
        if self._yt_client is None:
            self._yt_client = build(
                "youtube", "v3",
                credentials=self._auth.credentials(),
                cache_discovery=False,
            )
        return self._yt_client

    @resilient(attempts=4)
    def _search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        try:
            request = self._yt.search().list(
                q=query,
                part="snippet",
                type="video",
                eventType="live",           # active live streams ONLY
                videoEmbeddable="true",
                order="relevance",
                maxResults=max_results,
                safeSearch="none",
            )
            response = request.execute()
            return response.get("items", [])
        except HttpError as exc:
            # 403 quota / 5xx -> convert to retryable network error.
            raise requests.exceptions.ConnectionError(str(exc)) from exc

    @resilient(attempts=3)
    def _statistics(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch liveStreamingDetails + statistics to score candidates."""
        if not video_ids:
            return {}
        try:
            resp = self._yt.videos().list(
                part="statistics,liveStreamingDetails,snippet",
                id=",".join(video_ids),
            ).execute()
        except HttpError as exc:
            raise requests.exceptions.ConnectionError(str(exc)) from exc
        return {item["id"]: item for item in resp.get("items", [])}

    @staticmethod
    def _score(title: str, channel: str, view_count: int, concurrent: int) -> float:
        text = f"{title} {channel}".lower()
        score = float(max(view_count, concurrent))
        official = any(sig in text for sig in _OFFICIAL_SIGNALS)
        if official:
            score *= 5.0
        if any(neg in text for neg in _NEGATIVE_SIGNALS):
            score *= 0.05
        return score

    def find_live_stream(self, fixture: Fixture) -> LiveStream | None:
        """Run the full hunt + validation heuristic for one fixture."""
        candidates: dict[str, dict[str, Any]] = {}
        for query in fixture.search_queries():
            log.debug("YouTube live search: %r", query)
            for item in self._search(query):
                vid = item["id"]["videoId"]
                candidates.setdefault(vid, item)
            if len(candidates) >= 15:
                break

        if not candidates:
            log.info("No live streams found for %s", fixture.title)
            return None

        stats = self._statistics(list(candidates.keys()))

        best: LiveStream | None = None
        best_score = -1.0
        for vid, item in candidates.items():
            snippet = item["snippet"]
            detail = stats.get(vid, {})
            live_details = detail.get("liveStreamingDetails", {})
            # Skip anything that isn't *actually* still live.
            if "actualEndTime" in live_details:
                continue
            statistics = detail.get("statistics", {})
            view_count = int(statistics.get("viewCount", 0))
            concurrent = int(live_details.get("concurrentViewers", 0))
            channel = snippet.get("channelTitle", "")
            title = snippet.get("title", "")
            score = self._score(title, channel, view_count, concurrent)
            if score > best_score:
                best_score = score
                best = LiveStream(
                    video_id=vid,
                    channel_title=channel,
                    title=title,
                    view_count=max(view_count, concurrent),
                    is_official_ish=any(s in f"{title} {channel}".lower()
                                        for s in _OFFICIAL_SIGNALS),
                    watch_url=f"https://www.youtube.com/watch?v={vid}",
                )

        if best:
            log.info(
                "Selected live stream for %s: %r (%s) score=%.0f",
                fixture.title, best.title, best.channel_title, best_score,
            )
        return best

    @resilient(attempts=4)
    def resolve_manifest_url(self, watch_url: str) -> str:
        """Resolve a YouTube watch URL to a direct HLS/manifest URL for FFmpeg.

        FFmpeg cannot read youtube.com directly; yt-dlp extracts the underlying
        live HLS manifest (``m3u8``) which FFmpeg *can* demux.
        """
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            # Prefer an HLS rendition at <=1080p for efficient copy-muxing.
            "format": "best[protocol^=m3u8][height<=1080]/best[height<=1080]/best",
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(watch_url, download=False)

        # Live streams expose `manifest_url`; fall back to the chosen format URL.
        manifest = info.get("manifest_url")
        if manifest:
            return manifest
        if info.get("url"):
            return info["url"]
        for fmt in reversed(info.get("formats", [])):
            if fmt.get("url"):
                return fmt["url"]
        raise RuntimeError(f"Could not resolve a playable URL for {watch_url}")
