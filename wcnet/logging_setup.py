"""Structured, thread-aware logging used across every worker."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Idempotently configure root logging for the whole ecosystem."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    fmt = (
        "%(asctime)s | %(levelname)-7s | %(threadName)-18s | "
        "%(name)s | %(message)s"
    )
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    # Windows consoles default to cp1252 and crash on emoji/✔ glyphs in logs;
    # force UTF-8 so the daemon's status logs never blow up.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    root = logging.getLogger()
    root.setLevel(level.upper())

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Third-party libraries are noisy at INFO; pin them to WARNING.
    for noisy in ("googleapiclient", "google", "urllib3", "boto3", "botocore",
                  "s3transfer", "yt_dlp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
