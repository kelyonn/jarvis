"""Google sign-in for Gmail: OAuth (loopback + PKCE), token storage and refresh. No third-party SDK, just httpx.

Tokens live in ``~/.jarvis/google/<connector>.json`` (mode 0600). On first use, a token file left by an earlier setup in
``~/.openjarvis/connectors/`` is copied in, so you do not have to sign in again.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import shutil
import threading
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from jarvis_core import paths

log = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
GOOGLE_CONSOLE_URL = "https://console.cloud.google.com/apis/credentials"
REDIRECT_PORT = 8789
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

# Jarvis only reads mail and creates drafts (it cannot send), so it asks for the minimum.
GMAIL_ONLY_SCOPES = ["openid", "email", "https://www.googleapis.com/auth/gmail.modify"]
# Calendar (read and add events) and Drive (read only) are used by the Google tools; mail still cannot be sent.
GOOGLE_SCOPES = [*GMAIL_ONLY_SCOPES, "https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/drive.readonly"]
_ALLOWED_SCOPES = set(GOOGLE_SCOPES) | {
    "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile", "email", "profile",
}
_LEGACY_DIR = Path.home() / ".openjarvis" / "connectors"


def _file(connector: str) -> Path:
    p = paths.sub("google") / f"{connector}.json"
    if not p.exists():
        for legacy in (_LEGACY_DIR / f"{connector}.json", _LEGACY_DIR / "google.json"):
            if legacy.exists():
                try:
                    shutil.copy2(legacy, p)
                    p.chmod(0o600)
                    log.info("copied Google credentials from %s", legacy)
                except OSError:
                    pass
                break
    return p


def load_tokens(path: Path | str) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_tokens(path: Path | str, tokens: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(tokens, f)


class GoogleAuthError(RuntimeError):
    pass


def refresh_access_token(path: Path | str) -> str:
    tokens = load_tokens(path) or {}
    need = ("refresh_token", "client_id", "client_secret")
    if not all(tokens.get(k) for k in need):
        raise GoogleAuthError("Google sign-in is incomplete; run `jarvisd google`.")
    r = httpx.post(TOKEN_ENDPOINT, timeout=30, data={
        "client_id": tokens["client_id"], "client_secret": tokens["client_secret"],
        "refresh_token": tokens["refresh_token"], "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise GoogleAuthError(f"Google token refresh failed ({r.status_code}): {r.text[:200]}")
    new = r.json().get("access_token", "")
    if not new:
        raise GoogleAuthError("Google returned no access token.")
    tokens["access_token"] = tokens["token"] = new
    save_tokens(path, tokens)
    return new


def current_access_token(path: Path | str) -> str:
    t = load_tokens(path) or {}
    return t.get("access_token") or t.get("token") or ""


class GoogleSession:
    """Authenticated Google REST access."""

    def __init__(self, connector: str = "gmail") -> None:
        self._path = _file(connector)

    def connected(self) -> bool:
        return bool(current_access_token(self._path))

    def valid(self) -> bool:
        """True if Google still honours the saved refresh token (False once it was revoked or expired)."""
        try:
            refresh_access_token(self._path)
            return True
        except Exception as e:  # noqa: BLE001
            return "invalid_grant" not in str(e)  # offline or a Google outage is not proof of revocation

    def scopes(self) -> str:
        saved = str((load_tokens(self._path) or {}).get("scope", ""))
        if saved:
            return saved
        try:
            def info(token: str) -> httpx.Response:
                return httpx.get("https://oauth2.googleapis.com/tokeninfo", params={"access_token": token}, timeout=15)

            r = info(current_access_token(self._path))
            if r.status_code != 200:
                r = info(refresh_access_token(self._path))
            return str(r.json().get("scope", "")) if r.status_code == 200 else ""
        except Exception:  # noqa: BLE001 - offline or expired: "" means unknown
            return ""

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        extra = dict(kw.pop("headers", {}) or {})

        def go(token: str) -> httpx.Response:
            return httpx.request(method, url, headers={**extra, "Authorization": f"Bearer {token}"}, timeout=20, **kw)

        r = go(current_access_token(self._path))
        if r.status_code == 401:
            r = go(refresh_access_token(self._path))
        return r


def has_google_client(connector: str = "gmail") -> bool:
    t = load_tokens(_file(connector)) or {}
    return bool((t.get("client_id") and t.get("client_secret"))
                or (os.environ.get("JARVIS_GOOGLE_CLIENT_ID") and os.environ.get("JARVIS_GOOGLE_CLIENT_SECRET")))


def save_google_client(client_id: str, client_secret: str, connector: str = "gmail") -> None:
    p = _file(connector)
    t = load_tokens(p) or {}
    t.update(client_id=client_id.strip(), client_secret=client_secret.strip())
    save_tokens(p, t)


def google_extra_scopes(connector: str = "gmail") -> list[str]:
    """Granted scopes beyond what Jarvis needs (empty when minimal or unknown)."""
    granted = set(GoogleSession(connector).scopes().split())
    return sorted(granted - _ALLOWED_SCOPES) if granted else []


def reset_google(connector: str = "gmail") -> bool:
    """Revoke the grant and forget the tokens (the client ID and secret stay). True if Google confirmed."""
    p = _file(connector)
    saved = load_tokens(p) or {}
    token = saved.get("refresh_token") or saved.get("access_token")
    revoked = False
    if token:
        try:
            revoked = httpx.post(REVOKE_ENDPOINT, data={"token": token}, timeout=15).status_code == 200
        except httpx.HTTPError:
            revoked = False
    save_tokens(p, {k: v for k, v in saved.items() if k in ("client_id", "client_secret")})
    return revoked


def _client(connector: str) -> tuple[str, str]:
    t = load_tokens(_file(connector)) or {}
    cid = t.get("client_id") or os.environ.get("JARVIS_GOOGLE_CLIENT_ID", "")
    sec = t.get("client_secret") or os.environ.get("JARVIS_GOOGLE_CLIENT_SECRET", "")
    if not (cid and sec):
        raise GoogleAuthError("No Google client ID and secret saved yet.")
    return cid, sec


def run_google_oauth(connector: str = "gmail", scopes: list[str] | None = None, *, open_browser: bool = True,
                     timeout_s: float = 180.0) -> None:
    """Browser consent on a localhost callback (PKCE + state), then save the tokens."""
    scopes = scopes or GOOGLE_SCOPES
    client_id, client_secret = _client(connector)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = AUTH_ENDPOINT + "?" + urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code", "scope": " ".join(scopes),
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            if urlparse(self.path).path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            if q.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            elif "code" in q:
                result["code"] = q["code"][0]
            else:
                result["error"] = q.get("error", ["denied"])[0]
            ok = "code" in result
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body style='font-family:system-ui;text-align:center;padding:60px'>"
                             + (b"<h2>Jarvis is connected.</h2><p>You can close this tab.</p>" if ok
                                else b"<h2>Sign-in failed.</h2><p>Close this tab and try again.</p>") + b"</body></html>")
            done.set()

        def log_message(self, *_a: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    server.timeout = 1.0
    if open_browser:
        webbrowser.open(url)
    else:
        print(url)
    import time

    deadline = time.monotonic() + timeout_s
    while not done.is_set() and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        raise GoogleAuthError(f"Google sign-in did not complete ({result.get('error', 'timed out')}).")
    r = httpx.post(TOKEN_ENDPOINT, timeout=30, data={
        "code": result["code"], "client_id": client_id, "client_secret": client_secret, "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code", "code_verifier": verifier})
    r.raise_for_status()
    tok = r.json()
    if not tok.get("access_token"):
        raise GoogleAuthError("Google returned no access token.")
    save_tokens(_file(connector), {
        "access_token": tok["access_token"], "token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
        "token_type": tok.get("token_type", "Bearer"), "expires_in": tok.get("expires_in", 3600),
        "scope": tok.get("scope", ""), "client_id": client_id, "client_secret": client_secret})
