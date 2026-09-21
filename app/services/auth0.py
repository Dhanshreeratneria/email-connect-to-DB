"""Auth0 JWT validation and scope handling for the MCP resource."""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, Request

from app.config import settings


_claims: ContextVar[dict[str, Any] | None] = ContextVar("auth0_claims", default=None)
_jwks_lock = threading.Lock()
_jwks_cache: dict[str, Any] | None = None
_jwks_expires_at = 0.0
_JWKS_TTL_SECONDS = 3600


class Auth0ConfigurationError(RuntimeError):
    """Raised when Auth0 is not configured for a protected request."""


def _issuer() -> str:
    issuer = settings.auth0_issuer.strip()
    if issuer:
        return issuer.rstrip("/") + "/"
    domain = settings.auth0_domain.strip()
    return f"https://{domain.rstrip('/')}/" if domain else ""


def _jwks_uri() -> str:
    issuer = _issuer()
    return f"{issuer}.well-known/jwks.json" if issuer else ""


def _get_jwks() -> dict[str, Any]:
    global _jwks_cache, _jwks_expires_at

    now = time.monotonic()
    if _jwks_cache is not None and now < _jwks_expires_at:
        return _jwks_cache

    with _jwks_lock:
        now = time.monotonic()
        if _jwks_cache is not None and now < _jwks_expires_at:
            return _jwks_cache
        try:
            response = httpx.get(_jwks_uri(), timeout=5.0)
            response.raise_for_status()
            jwks = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise Auth0ConfigurationError("Unable to retrieve Auth0 signing keys") from exc
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise Auth0ConfigurationError("Auth0 JWKS response is invalid")
        _jwks_cache = jwks
        _jwks_expires_at = time.monotonic() + _JWKS_TTL_SECONDS
        return jwks


def validate_token(token: str) -> dict[str, Any]:
    """Validate an Auth0 RS256 access token and return its claims."""
    issuer = _issuer()
    audience = settings.auth0_api_audience.strip()
    if not issuer or not audience or not settings.auth0_client_id.strip():
        raise Auth0ConfigurationError("Auth0 is not configured")

    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not kid:
            raise jwt.InvalidTokenError("Unexpected Auth0 token header")

        key_data = next(
            (key for key in _get_jwks()["keys"] if key.get("kid") == kid),
            None,
        )
        if key_data is None:
            raise jwt.InvalidTokenError("Auth0 signing key not found")

        claims = jwt.decode(
            token,
            jwt.algorithms.RSAAlgorithm.from_jwk(key_data),
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iss", "aud"]},
        )
        azp = claims.get("azp")
        if azp is not None and azp != settings.auth0_client_id:
            raise jwt.InvalidTokenError("Auth0 client is not authorized")
        return claims
    except (jwt.InvalidTokenError, Auth0ConfigurationError) as exc:
        raise Auth0ConfigurationError("Invalid Auth0 access token") from exc


def safe_validate_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        return validate_token(token)
    except Auth0ConfigurationError:
        return None


def scopes_from_claims(claims: dict[str, Any]) -> set[str]:
    scope = claims.get("scope", "")
    if isinstance(scope, str):
        scopes = set(scope.split())
    elif isinstance(scope, list):
        scopes = {str(item) for item in scope}
    else:
        scopes = set()
    permissions = claims.get("permissions", [])
    if isinstance(permissions, list):
        scopes.update(str(item) for item in permissions)
    return scopes


def set_claims(claims: dict[str, Any] | None):
    return _claims.set(claims)


def reset_claims(token) -> None:
    _claims.reset(token)


def current_scopes() -> set[str]:
    return scopes_from_claims(_claims.get() or {})


def require_scope(scope: str) -> None:
    if scope not in current_scopes():
        raise PermissionError(f"Missing required scope: {scope}")


def unauthorized_response_headers(base_url: str, error: str = "invalid_token") -> dict[str, str]:
    return {
        "WWW-Authenticate": (
            f'Bearer realm="mcp", error="{error}", '
            f'resource_metadata="{base_url.rstrip("/")}/.well-known/oauth-protected-resource"'
        )
    }


async def require_http_scope(request: Request, scope: str) -> None:
    """FastAPI dependency for attachment HTTP endpoints."""
    authorization = request.headers.get("authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    try:
        claims = validate_token(token) if token else None
        if claims is None:
            raise Auth0ConfigurationError("Missing bearer token")
        if scope not in scopes_from_claims(claims):
            raise HTTPException(
                status_code=403,
                detail="Insufficient scope",
                headers=unauthorized_response_headers(str(request.base_url), "insufficient_scope"),
            )
    except Auth0ConfigurationError as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing access token",
            headers=unauthorized_response_headers(str(request.base_url)),
        ) from exc


def http_scope(scope: str):
    async def dependency(request: Request) -> None:
        await require_http_scope(request, scope)

    return dependency
