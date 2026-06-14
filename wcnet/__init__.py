"""WCNET — autonomous World Cup short-form video network.

A self-driving pipeline that discovers live World Cup matches, hunts their
live YouTube streams, detects in-match highlight events, and renders a 9:16
short-form clip around each event to local files. Publishing is intentionally
out of scope — the pipeline stops at the finished .mp4 in data/clips/.
"""

__version__ = "1.0.0"

# Use the operating-system trust store for TLS verification. This makes Python
# trust the same CAs the OS does (incl. corporate proxy / VPN / AV roots that
# intercept TLS), which certifi's static bundle does not know about. Injecting
# here — at first import of the package — guarantees it runs before any SSL
# context is created by requests / google-api-client / boto3 / yt-dlp.
try:  # truststore is optional; on hosts with clean CA chains it isn't needed.
    import truststore as _truststore

    _truststore.inject_into_ssl()
except Exception:  # noqa: BLE001 - never let TLS setup break startup
    pass
