"""One-time helper: obtain a TikTok long-lived refresh token.

TikTok's Content Posting API needs a user access token. Run this once, complete
the consent in your browser, and paste the resulting refresh_token into your
.env (TIKTOK_REFRESH_TOKEN). After that the daemon refreshes silently forever.

Prerequisites:
  * Your app's redirect URI in the TikTok developer portal must include
    http://localhost:8723/callback
  * Scopes: video.publish (and video.upload), user.info.basic

Usage:
    python scripts/tiktok_oauth_bootstrap.py
"""

from __future__ import annotations

import hashlib
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

sys.path.insert(0, ".")
from wcnet.config import get_settings  # noqa: E402

REDIRECT_URI = "http://localhost:8723/callback"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
SCOPES = "user.info.basic,video.publish,video.upload"

_code: str | None = None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        global _code
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Authorized. You can close this tab.</h2>")

    def log_message(self, *_args):  # silence
        pass


def _make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge).

    NOTE: TikTok deviates from RFC 7636 — the code_challenge must be the
    *hex* SHA-256 digest of the verifier, not base64url. (Their JS sample uses
    CryptoJS.SHA256(verifier).toString(), which is hex.)
    """
    verifier = secrets.token_hex(48)  # 96 chars, within the 43–128 range
    challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
    return verifier, challenge


def main() -> int:
    settings = get_settings()
    state = secrets.token_urlsafe(16)
    code_verifier, code_challenge = _make_pkce()
    auth_params = {
        "client_key": settings.tiktok_client_key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"
    print("Opening browser for TikTok consent...\n", url)
    webbrowser.open(url)

    server = HTTPServer(("localhost", 8723), _Handler)
    while _code is None:
        server.handle_request()

    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "code": _code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    print("\n=== TikTok tokens ===")
    print("access_token :", data.get("access_token", "")[:24], "...")
    print("refresh_token:", data.get("refresh_token"))
    print("\nPaste the refresh_token into .env as TIKTOK_REFRESH_TOKEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
