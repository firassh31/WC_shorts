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


class FallbackSession(requests.Session):
    """certifi-by-default session that retries via the OS trust store on ANY
    TLS error.

    A VPN/AV/proxy that intercepts TLS presents a root that lives in the OS
    store but not in certifi. Depending on the middlebox that surfaces either as
    a clean cert-verify failure OR as a handshake EOF/reset — so we fall back on
    *any* SSLError. certifi is always tried FIRST, so hosts the OS/SChannel path
    mishandles still work when no middlebox is present; if both paths fail, the
    original certifi error is raised so the caller's retry stays on that path.
    """

    def __init__(self) -> None:
        super().__init__()
        self._os_session: requests.Session | None = None

    def _os(self) -> requests.Session:
        if self._os_session is None:
            s = requests.Session()
            s.headers = self.headers
            s.mount("https://", _OSTrustAdapter())
            self._os_session = s
        return self._os_session

    def request(self, method, url, **kwargs):  # type: ignore[override]
        try:
            return super().request(method, url, **kwargs)
        except requests.exceptions.SSLError as certifi_err:
            try:
                return self._os().request(method, url, **kwargs)
            except requests.exceptions.SSLError:
                raise certifi_err


def make_session() -> FallbackSession:
    return FallbackSession()
