"""Cloudflare R2 (S3-compatible) uploader — public URL host for IG ingestion.

Instagram's Graph API ingests Reels from a publicly reachable URL, so we push
the rendered MP4 to an R2 bucket via boto3 and hand back the public URL.
"""

from __future__ import annotations

import logging
from pathlib import Path

import boto3
from botocore.config import Config

from ..config import Settings
from ..utils.retry import resilient

log = logging.getLogger("wcnet.publish.r2")


class R2Uploader:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._client = None  # lazy

    def is_configured(self) -> bool:
        return bool(
            self._s.r2_account_id
            and self._s.r2_access_key_id
            and self._s.r2_secret_access_key
            and self._s.r2_endpoint
            and self._s.r2_public_base_url
        )

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=self._s.r2_endpoint,
                aws_access_key_id=self._s.r2_access_key_id,
                aws_secret_access_key=self._s.r2_secret_access_key,
                region_name="auto",
                config=Config(signature_version="s3v4",
                              retries={"max_attempts": 5, "mode": "standard"}),
            )
        return self._client

    @resilient(attempts=4)
    def upload(self, local_path: str) -> str:
        """Upload the file and return its public URL."""
        key = f"clips/{Path(local_path).name}"
        client = self._get_client()
        client.upload_file(
            local_path,
            self._s.r2_bucket,
            key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
        url = f"{self._s.r2_public_base_url.rstrip('/')}/{key}"
        log.info("Uploaded to R2: %s", url)
        return url
