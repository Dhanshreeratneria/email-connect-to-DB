import base64
from datetime import datetime

from sqlalchemy import or_, select
from mcp.server.fastmcp import FastMCP

from app.database import SessionLocal
from app.models.email import Email, EmailDelivery, EmailAttachment
from app.services.gmail_service import gmail, get_attachment
from app.services.auth_service import decrypt_credentials
from app.utils.gmail import decode_base64_urlsafe_bytes

mcp = FastMCP(
    "Gmail Email Search",
    instructions="Read-only email search. Never expose OAuth tokens or secrets."
)


def serialize(e: Email):
    """Serialize email including attachment metadata."""
    attachments = []

    # Get attachment records stored in PostgreSQL
    if hasattr(e, "stored_attachments"):
        for attachment in e.stored_attachments:
            attachments.append({
                "id": attachment.id,
                "gmail_attachment_id": attachment.gmail_attachment_id,
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "size": attachment.size,
                "stored_in_postgresql": attachment.content is not None,
                "content_available": attachment.content is not None,
                "stored_content_size": (
                    len(attachment.content)
                    if attachment.content is not None
                    else 0
                ),
            })

    return {
        "id": e.id,
        "rfc_message_id": e.rfc_message_id,
        "thread_id": e.thread_id,
        "sender_name": e.sender_name,
        "sender_email": e.sender_email,
        "recipients": e.recipients,
        "subject": e.subject,
        "body_text": e.body_text,
        "received_at": e.received_at.isoformat(),
        "labels": e.labels,
        "category": e.category,
        "has_attachments": e.has_attachments,
        "attachments": attachments,
    }


def query(stmt):
    db = SessionLocal()
    try:
        return [serialize(e) for e in db.scalars(stmt).all()]
    finally:
        db.close()


@mcp.tool()
def search_emails(
    query_text: str,
    limit: int | None = None
) -> list[dict]:
    """Search sender, subject, and text body. Omit limit for all matches."""

    q = f"%{query_text}%"

    stmt = (
        select(Email)
        .where(
            or_(
                Email.sender_email.ilike(q),
                Email.subject.ilike(q),
                Email.body_text.ilike(q),
            )
        )
        .order_by(Email.received_at.desc())
    )

    if limit is not None:
        stmt = stmt.limit(limit)

    return query(stmt)


@mcp.tool()
def get_email(gmail_message_id: str) -> dict | None:
    """Get one stored email by its Gmail message ID."""

    db = SessionLocal()

    try:
        delivery = db