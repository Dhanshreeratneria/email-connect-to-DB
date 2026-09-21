"""Database-backed MCP client credentials and admin authorization."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.email import AdminClient, AdminClientPermission, AdminToken

MCP_TOKEN_PREFIX = "mcp_"
MCP_PERMISSIONS = {"read:emails", "read:attachments", "download:attachments"}
ADMIN_PERMISSION = "admin:manage"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(db: Session, client: AdminClient) -> tuple[AdminToken, str]:
    raw_token = MCP_TOKEN_PREFIX + secrets.token_urlsafe(32)
    record = AdminToken(
        client_id=client.id,
        token_hash=hash_token(raw_token),
        token_prefix=raw_token[:20],
    )
    db.add(record)
    db.flush()
    return record, raw_token


def validate_token(db: Session, raw_token: str | None) -> dict | None:
    if not raw_token or not raw_token.startswith(MCP_TOKEN_PREFIX):
        return None
    digest = hash_token(raw_token)
    record = db.scalar(select(AdminToken).where(AdminToken.token_hash == digest))
    if (
        record is None
        or record.revoked_at is not None
        or record.client is None
        or not record.client.enabled
        or not hmac.compare_digest(record.token_hash, digest)
    ):
        return None
    return {
        "sub": f"client:{record.client.id}",
        "client_id": record.client.id,
        "client_name": record.client.name,
        "scope": " ".join(permission.permission for permission in record.client.permissions),
        "token_id": record.id,
    }


def revoke_token(token: AdminToken) -> None:
    token.revoked_at = datetime.now(timezone.utc)


def set_permissions(client: AdminClient, permissions: set[str]) -> None:
    client.permissions.clear()
    client.permissions.extend(
        AdminClientPermission(permission=permission)
        for permission in sorted(permissions & MCP_PERMISSIONS)
    )