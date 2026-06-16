"""Run yt-dlp with the OS trust store injected.

The live recorder shells out to yt-dlp. On machines whose TLS is intercepted by
a VPN / antivirus / corporate proxy, yt-dlp's default certifi bundle can't find
the intercepting root CA and fails with CERTIFICATE_VERIFY_FAILED. Injecting the
operating-system trust store (which *does* hold that root) fixes verification
without disabling it.

Invoked by StreamRecorder as:  python -m wcnet._ytdlp_run <yt-dlp args>
"""

from __future__ import annotations

import contextlib

with contextlib.suppress(Exception):
    import truststore

    truststore.inject_into_ssl()

from yt_dlp import main  # noqa: E402

if __name__ == "__main__":
    main()
