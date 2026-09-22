"""Database-backed MCP client credentials and admin authorization."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.email import AdminClient, AdminClientPermission, AdminToken

logger = logging.getLogger(__name__)

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
        logger.info("MCP token validation failure: invalid token format")
        return None
    digest = hash_token(raw_token)
    record = db.scalar(select(AdminToken).where(AdminToken.token_hash == digest))
    if record is None:
        logger.info("MCP token validation failure: token not found")
        return None
    if record.revoked_at is not None:
        logger.info("MCP token validation failure: token revoked token_id=%s", record.id)
        return None
    if record.client is None or not record.client.enabled:
        logger.info(
            "MCP token validation failure: client disabled or missing token_id=%s",
            record.id,
        )
        return None
    if not hmac.compare_digest(record.token_hash, digest):
        logger.info("MCP token validation failure: token hash mismatch token_id=%s", record.id)
        return None
    permissions = sorted(permission.permission for permission in record.client.permissions)
    logger.info(
        "MCP token validation success client_id=%s permissions=%s",
        record.client.id,
        permissions,
    )
    return {
        "sub": f"client:{record.client.id}",
        "client_id": record.client.id,
        "client_name": record.client.name,
        "scope": " ".join(permissions),
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