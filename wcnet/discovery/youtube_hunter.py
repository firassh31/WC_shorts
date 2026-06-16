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
    "free", "watch now", "click here", "link in", "efootball",
    "fifa 23", "fifa 24", "fc 24", "fc 25",  # video games, not the match
    "watchalong", "watch along", "watch-along", "watch party", "watchparty",
    "reaction", "preview", "build-up", "buildup", "press conference",
    "presser", "analysis", "studio", "discussion", "debate", "podcast",
    "co-stream", "costream", "live scores", "live score", "score update",
    "scoreboard", "radio", "news", "talk show", "talkshow",
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
            for item in self._search(query, max_results=15):
                vid = item["id"]["videoId"]
                candidates.setdefault(vid, item)
            if len(candidates) >= 25:
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
