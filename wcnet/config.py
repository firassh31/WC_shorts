"""LIFECYCLE STAGE 1 (a) — Centralized configuration.

A single, validated source of truth for every credential and tuning knob in
the ecosystem. Built on pydantic-settings so values are read from the process
environment (and a local ``.env`` during development), type-coerced, and
validated once at startup. Nothing else in the codebase touches ``os.environ``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed, validated application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── General ──────────────────────────────────────────────────────────
    env: Literal["production", "staging", "development"] = Field(
        default="production", alias="WCNET_ENV"
    )
    log_level: str = Field(default="INFO", alias="WCNET_LOG_LEVEL")
    data_dir: Path = Field(default=Path("./data"), alias="WCNET_DATA_DIR")

    # ── Football data ────────────────────────────────────────────────────
    football_api_key: str = Field(default="", alias="FOOTBALL_API_KEY")
    football_api_host: str = Field(
        default="v3.football.api-sports.io", alias="FOOTBALL_API_HOST"
    )
    football_api_via_rapidapi: bool = Field(
        default=False, alias="FOOTBALL_API_VIA_RAPIDAPI"
    )
    football_league_id: int = Field(default=1, alias="FOOTBALL_LEAGUE_ID")
    football_season: int = Field(default=2026, alias="FOOTBALL_SEASON")
    football_poll_seconds: int = Field(default=60, alias="FOOTBALL_POLL_SECONDS")

    # ── YouTube ──────────────────────────────────────────────────────────
    youtube_api_key: str = Field(default="", alias="YOUTUBE_API_KEY")
    youtube_client_secrets: Path = Field(
        default=Path("./secrets/client_secrets.json"),
        alias="YOUTUBE_CLIENT_SECRETS",
    )
    youtube_token_cache: Path = Field(
        default=Path("./data/youtube_token.json"), alias="YOUTUBE_TOKEN_CACHE"
    )
    youtube_category_id: str = Field(default="17", alias="YOUTUBE_CATEGORY_ID")
    youtube_privacy: Literal["public", "unlisted", "private"] = Field(
        default="public", alias="YOUTUBE_PRIVACY"
    )

    # ── TikTok ───────────────────────────────────────────────────────────
    tiktok_client_key: str = Field(default="", alias="TIKTOK_CLIENT_KEY")
    tiktok_client_secret: str = Field(default="", alias="TIKTOK_CLIENT_SECRET")
    tiktok_refresh_token: str = Field(default="", alias="TIKTOK_REFRESH_TOKEN")
    tiktok_token_cache: Path = Field(
        default=Path("./data/tiktok_token.json"), alias="TIKTOK_TOKEN_CACHE"
    )
    tiktok_privacy_level: str = Field(
        default="PUBLIC_TO_EVERYONE", alias="TIKTOK_PRIVACY_LEVEL"
    )

    # ── Instagram / Meta Graph ───────────────────────────────────────────
    ig_user_id: str = Field(default="", alias="IG_USER_ID")
    ig_access_token: str = Field(default="", alias="IG_ACCESS_TOKEN")
    ig_graph_version: str = Field(default="v19.0", alias="IG_GRAPH_VERSION")

    # ── Cloudflare R2 ────────────────────────────────────────────────────
    r2_account_id: str = Field(default="", alias="R2_ACCOUNT_ID")
    r2_access_key_id: str = Field(default="", alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: str = Field(default="", alias="R2_SECRET_ACCESS_KEY")
    r2_bucket: str = Field(default="wcnet-clips", alias="R2_BUCKET")
    r2_endpoint: str = Field(default="", alias="R2_ENDPOINT")
    r2_public_base_url: str = Field(default="", alias="R2_PUBLIC_BASE_URL")

    # ── Capture / render ─────────────────────────────────────────────────
    capture_pre_seconds: int = Field(default=18, alias="CAPTURE_PRE_SECONDS")
    capture_post_seconds: int = Field(default=10, alias="CAPTURE_POST_SECONDS")
    buffer_window_seconds: int = Field(default=240, alias="BUFFER_WINDOW_SECONDS")
    segment_seconds: int = Field(default=2, alias="SEGMENT_SECONDS")
    render_mode: Literal["blur_pad", "center_crop"] = Field(
        default="blur_pad", alias="RENDER_MODE"
    )
    ffmpeg_binary: str = Field(default="ffmpeg", alias="FFMPEG_BINARY")

    # ── Validators ───────────────────────────────────────────────────────
    @field_validator("data_dir", "youtube_client_secrets", "youtube_token_cache",
                     "tiktok_token_cache", mode="after")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return Path(os.path.expandvars(str(value))).expanduser()

    @field_validator("capture_post_seconds")
    @classmethod
    def _clip_under_60(cls, post: int, info) -> int:
        pre = info.data.get("capture_pre_seconds", 0)
        if pre + post >= 60:
            raise ValueError(
                "capture_pre_seconds + capture_post_seconds must be < 60 "
                "to satisfy short-form platform limits."
            )
        return post

    # ── Derived paths ────────────────────────────────────────────────────
    @computed_field  # type: ignore[prop-decorator]
    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "wcnet_state.sqlite3"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def buffer_dir(self) -> Path:
        return self.data_dir / "buffer"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def clip_total_seconds(self) -> int:
        return self.capture_pre_seconds + self.capture_post_seconds

    def ensure_dirs(self) -> None:
        """Create every runtime directory the ecosystem expects."""
        for path in (self.data_dir, self.buffer_dir, self.clips_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide singleton settings object."""
    settings = Settings()  # type: ignore[call-arg]
    settings.ensure_dirs()
    return settings
