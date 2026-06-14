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
# Titles/channels that are NOT the live match feed — talk shows, watchalongs,
# reactions, previews, score tickers, games, etc. Any hit hard-excludes the
# candidate (better to find nothing than record the wrong video).
_NEGATIVE_SIGNALS = (
    "free", "watch now", "click", "link in", "pes", "fifa 2", "efootball",
    "watchalong", "watch along", "watch-along", "watch party", "watchparty",
    "reaction", "react", "preview", "build-up", "buildup", "press conference",
    "presser", "analysis", "studio", "discussion", "debate", "podcast",
    "co-stream", "costream", "live scores", "live score", "score update",
    "scoreboard", "radio", "news", "news18", "talk show", "talkshow",
    "predicted", "prediction", "fan tv", "fantv", "highlights", "recap",
)


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
    def _is_match_feed(title: str, channel: str, team_tokens: set[str]) -> bool:
        """True only if this looks like the actual match feed (not a talk show).

        Rejects any candidate carrying a non-match signal, and requires the
        title/channel to actually mention one of the playing teams.
        """
        text = f"{title} {channel}".lower()
        if any(neg in text for neg in _NEGATIVE_SIGNALS):
            return False
        if team_tokens and not any(tok in text for tok in team_tokens):
            return False
        return True

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

    def find_live_stream(self, fixture: Fixture,
                         exclude: set[str] | None = None) -> LiveStream | None:
        """Run the full hunt + validation heuristic for one fixture.

        ``exclude`` is a set of video ids to skip (streams already tried and
        found dead), so failover picks a different feed.
        """
        exclude = exclude or set()
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

        # Tokens of the playing teams (skip short words) for relevance checking.
        team_tokens = {
            w for t in (fixture.home_team, fixture.away_team)
            for w in t.lower().replace("-", " ").split() if len(w) > 3
        }

        best: LiveStream | None = None
        best_score = -1.0
        rejected = 0
        for vid, item in candidates.items():
            if vid in exclude:
                continue
            snippet = item["snippet"]
            detail = stats.get(vid, {})
            live_details = detail.get("liveStreamingDetails", {})
            # Skip anything that isn't *actually* still live.
            if "actualEndTime" in live_details:
                continue
            channel = snippet.get("channelTitle", "")
            title = snippet.get("title", "")
            # HARD filter: reject talk-shows / watchalongs / off-topic streams.
            if not self._is_match_feed(title, channel, team_tokens):
                rejected += 1
                continue
            statistics = detail.get("statistics", {})
            view_count = int(statistics.get("viewCount", 0))
            concurrent = int(live_details.get("concurrentViewers", 0))
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
        else:
            log.info(
                "No verified match feed for %s (%d candidate(s) rejected as "
                "non-match: talk-shows / watchalongs / off-topic).",
                fixture.title, rejected,
            )
        return best

    # ── Creative-Commons clip search (legally reusable REAL footage) ─────────
    @resilient(attempts=4)
    def _search_videos(self, query: str, creative_commons: bool,
                       max_results: int = 20) -> list[dict[str, Any]]:
        params: dict[str, Any] = dict(
            q=query, part="snippet", type="video", maxResults=max_results,
            order="viewCount", safeSearch="none",
        )
        if creative_commons:
            # Only clips the uploader licensed for reuse under CC BY.
            params["videoLicense"] = "creativeCommon"
        try:
            return self._yt.search().list(**params).execute().get("items", [])
        except HttpError as exc:
            raise requests.exceptions.ConnectionError(str(exc)) from exc

    def find_clip(self, query: str, creative_commons: bool = True) -> LiveStream | None:
        """Find the best reusable clip for a query.

        With ``creative_commons=True`` (default) only CC-BY licensed videos are
        returned — real footage the uploader has explicitly permitted you to
        reuse (attribution required). Volume is lower than the open web, but it
        is legitimate and Content-ID-safe when the source license is genuine.
        """
        items = self._search_videos(query, creative_commons)
        if not items:
            log.info("No %sclips found for %r",
                     "CC-licensed " if creative_commons else "", query)
            return None
        ids = [it["id"]["videoId"] for it in items]
        stats = self._statistics(ids)
        best: LiveStream | None = None
        best_v = -1
        for it in items:
            vid = it["id"]["videoId"]
            sn = it["snippet"]
            vc = int(stats.get(vid, {}).get("statistics", {}).get("viewCount", 0))
            if vc > best_v:
                best_v = vc
                best = LiveStream(
                    video_id=vid, channel_title=sn.get("channelTitle", ""),
                    title=sn.get("title", ""), view_count=vc,
                    is_official_ish=False,
                    watch_url=f"https://www.youtube.com/watch?v={vid}",
                )
        if best:
            log.info("Selected CC clip: %r (%s) views=%d",
                     best.title, best.channel_title, best_v)
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
