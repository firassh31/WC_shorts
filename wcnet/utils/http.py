"""Resilient HTTPS session with automatic trust-store fallback.

Default verification uses certifi (works for most hosts, including ones whose
TLS the Windows SChannel/OS path mishandles, e.g. worldcup26.ir). If a request
fails with a TLS verification error — the signature of a VPN/proxy/AV that
MITMs TLS with a root only present in the OS store — we transparently retry the
request through the operating-system trust store.

This avoids the global ``truststore.inject_into_ssl()`` that fixes one host but
breaks another; each host gets whichever trust path actually works.
"""

from __future__ import annotations

import ssl

import requests
from requests.adapters import HTTPAdapter


class _OSTrustAdapter(HTTPAdapter):
    """HTTPAdapter that verifies against the operating-system trust store."""

    def init_poolmanager(self, *args, **kwargs):
        try:
            import truststore
            kwargs["ssl_context"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:  # noqa: BLE001 - fall back to default verification
            pass
        super().init_poolmanager(*args, **kwargs)


# Signatures of a certificate *trust* failure (vs. a transient connection drop).
# Only these warrant the OS-trust-store fallback; a connection EOF/reset must be
# retried on the SAME (certifi) path, not detoured to the OS store.
_VERIFY_FAILURE_MARKERS = (
    "certificate_verify_failed",
    "unable to get local issuer",
    "self signed certificate",
    "self-signed certificate",
    "certificate has expired",
)


class FallbackSession(requests.Session):
    """certifi-by-default session that retries via the OS trust store ONLY on a
    certificate-verification failure (e.g. a VPN/proxy MITM root). Transient
    connection errors (SSL EOF / reset) are re-raised so the caller's normal
    retry runs on the working certifi path."""

    def __init__(self) -> None:
        super().__init__()
        self._os_session: requests.Session | None = None

    def request(self, method, url, **kwargs):  # type: ignore[override]
        try:
            return super().request(method, url, **kwargs)
        except requests.exceptions.SSLError as exc:
            if not any(m in str(exc).lower() for m in _VERIFY_FAILURE_MARKERS):
                raise  # connection-level blip — let the normal retry handle it
            if self._os_session is None:
                s = requests.Session()
                s.headers = self.headers
                s.mount("https://", _OSTrustAdapter())
                self._os_session = s
            return self._os_session.request(method, url, **kwargs)


def make_session() -> FallbackSession:
    return FallbackSession()
