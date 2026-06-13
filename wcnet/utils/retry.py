"""SYSTEM RESILIENCY — retry & guard primitives used everywhere.

Every network call, file I/O boundary and external-API segment is wrapped so
that transient failures (timeouts, 5xx, rate limits, brief stream drops)
become automatic, backed-off retries instead of crashing the daemon.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Type, TypeVar

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger("wcnet.retry")

T = TypeVar("T")

# Exceptions that are safe to retry: anything network-ish.
RETRYABLE_EXCEPTIONS: tuple[Type[BaseException], ...] = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    ConnectionResetError,
    TimeoutError,
    OSError,
)


def resilient(
    *,
    attempts: int = 5,
    base_wait: float = 2.0,
    max_wait: float = 60.0,
    exceptions: tuple[Type[BaseException], ...] = RETRYABLE_EXCEPTIONS,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator: exponential-backoff retry for transient failures."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        wrapped = retry(
            reraise=True,
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=base_wait, max=max_wait),
            retry=retry_if_exception_type(exceptions),
            before_sleep=before_sleep_log(log, logging.WARNING),
        )(func)
        return functools.wraps(func)(wrapped)

    return decorator


def safe(default: Any = None, *, label: str | None = None):
    """Decorator: swallow *any* exception, log it, and return ``default``.

    Used for non-critical best-effort segments (e.g. one publisher failing
    should never take down the others or the monitoring loop).
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T | Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T | Any:
            try:
                return func(*args, **kwargs)
            except Exception:  # noqa: BLE001 - intentional broad guard
                log.exception("Suppressed failure in %s", label or func.__name__)
                return default

        return wrapper

    return decorator
