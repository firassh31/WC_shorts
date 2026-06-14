"""WCNET — autonomous World Cup short-form video network.

A self-driving pipeline that discovers live World Cup matches, hunts their
live YouTube streams, detects in-match highlight events, and renders a 9:16
short-form clip around each event to local files. Publishing is intentionally
out of scope — the pipeline stops at the finished .mp4 in data/clips/.
"""

__version__ = "1.0.0"

# NOTE: TLS verification is handled per-session in wcnet.utils.http
# (certifi by default, OS trust store as fallback). We intentionally do NOT
# call truststore.inject_into_ssl() globally — forcing the OS/SChannel path
# fixes some hosts (VPN/proxy MITM) but breaks others (e.g. worldcup26.ir).
