"""Shared publisher contract & result type."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import RenderedClip


@dataclass
class PublishResult:
    platform: str
    ok: bool
    remote_id: str | None = None
    error: str | None = None


class Publisher(ABC):
    """Common interface every platform publisher implements."""

    platform: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """True if all required credentials for this platform are present."""

    @abstractmethod
    def publish(self, clip: RenderedClip) -> PublishResult:
        """Upload + publish the clip; never raises — returns a result."""
