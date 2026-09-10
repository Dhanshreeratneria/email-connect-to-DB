"""
Minimal OAuth 2.1 authorization server used ONLY to gate access to the
/mcp endpoint for the Claude.ai connector. Separate from /auth/google,
which is the real Google OAuth that lets the app read Gmail.

Flow: Claude.ai hits /authorize -> we bounce the browser through the
SAME Google login screen -> once Google confirms who it is, we check
their email against CONNECTOR_ALLOWED_EMAIL before handing Claude.ai a
code. Anyone else's Google login is rejected here.

State lives in memory. Render's free tier can restart the process
after idling, which clears these dicts -- that only means an in-flight
login (seconds long) could drop, or an old access token gets forgotten
(Claude.ai just silently redoes the OAuth flow). Not a security issue,
just a "move to a database if you outgrow the free tier" note.
"""

import base64
import hashlib
import secrets
import time

_clients: dict[str, dict] = {}
_pending: dict[str, dict] = {}
_google_state_to_conn: dict[str, str] = {}
_auth_codes: dict[str, dict] = {}
_access_tokens: dict[str, dict] = {}

AUTH_CODE_TTL = 600
ACCESS_TOKEN_TTL = 3600


def register_client(redirect_uris: list[str]) -> str:
    client_id = secrets.token_urlsafe(16)
    _clients[client_id] = {"redirect_uris": redirect_uris}
    return client_id


def start_authorization(*, client_id, redirect_uri, code_challenge,
                         code_challenge_method, state, resource=None) -> str:
    conn_state = secrets.token_urlsafe(24)
    _pending[conn_state] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method or "S256",
        "state": state,
        "resource": resource,
        "created": time.time(),
    }
    return conn_state


def map_google_state(google_state: str, conn_state: str) -> None:
    _google_state_to_conn[google_state] = conn_state


def resolve_google_state(google_state: str) -> dict | None:
    conn_state = _google_state_to_conn.pop(google_state, None)
    if not conn_state:
        return None
    return _pending.pop(conn_state, None)


def issue_auth_code(pending: dict, google_email: str) -> str:
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "redirect_uri": pending["redirect_uri"],
        "code_challenge": pending["code_challenge"],
        "google_email": google_email,
        "created": time.time(),
    }
    return code


def redeem_auth_code(code: str, code_verifier: str, redirect_uri: str) -> dict | None:
    data = _auth_codes.pop(code, None)
    if not data:
        return None
    if time.time() - data["created"] > AUTH_CODE_TTL:
        return None
    if data["redirect_uri"] != redirect_uri:
        return None
    if not code_verifier:
        return None
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip("=")
    if expected != data["code_challenge"]:
        return None
    return data


def issue_access_token(google_email: str) -> dict:
    token = secrets.token_urlsafe(32)
    _access_tokens[token] = {"google_email": google_email, "created": time.time()}
    return {"access_token": token, "token_type": "bearer", "expires_in": ACCESS_TOKEN_TTL}


def verify_access_token(token: str) -> dict | None:
    data = _access_tokens.get(token)
    if not data:
        return None
    if time.time() - data["created"] > ACCESS_TOKEN_TTL:
        _access_tokens.pop(token, None)
        return None
    return data
