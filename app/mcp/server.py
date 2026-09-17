from datetime import datetime
from sqlalchemy import or_, select
from mcp.server.fastmcp import FastMCP
from app.database import SessionLocal
from app.models.email import Email, EmailDelivery

mcp = FastMCP(
    "Gmail Email Search",
    instructions="Read-only email search. Never expose OAuth tokens or secrets."
)

def group_recipients(recipients: list) -> dict[str, list[dict[str, str | None]]]:
    """
    Groups the stored recipients list by delivery type ("to"/"cc"/"bcc")
    so callers get a ready-to-use breakdown instead of a flat list they
    have to filter themselves.

    Tolerates rows synced before recipients carried a "type" (older data
    stored as plain email strings) by bucketing those under "unknown"
    rather than dropping or mis-tagging them.
    """
    grouped: dict[str, list[dict[str, str | None]]] = {
        "to": [], "cc": [], "bcc": [], "unknown": []
    }

    for r in recipients or []:
        if isinstance(r, dict) and r.get("type") in ("to", "cc", "bcc"):
            grouped[r["type"]].append(r)
        elif isinstance(r, dict):
            grouped["unknown"].append(r)
        else:
            grouped["unknown"].append({"name": None, "email": r, "type": None})

    return grouped


def serialize(e: Email):
    recipients = group_recipients(e.recipients)

    return {
        "id": e.id,
        "rfc_message_id": e.rfc_message_id,
        "thread_id": e.thread_id,
        "sender_name": e.sender_name,
        "sender_email": e.sender_email,
        "recipients": recipients,
        "recipient_count": sum(len(v) for v in recipients.values()),
        "subject": e.subject,
        "body_text": e.body_text,
        "received_at": e.received_at.isoformat(),
        "labels": e.labels,
        "category": e.category,
        "has_attachments": e.has_attachments,
        # Each attachment dict already carries a short "type" (image,
        # pdf, document, spreadsheet, archive, ...) derived from its
        # mime_type by email_parser.classify_attachment_type, in
        # addition to the raw mime_type itself.
        "attachments": e.attachments,
    }

def query(stmt):
    db = SessionLocal()
    try:
        return [serialize(e) for e in db.scalars(stmt).all()]
    finally:
        db.close()

@mcp.tool()
def search_emails(query_text: str, limit: int | None = None) -> list[dict]:
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
    """Get one stored email by its Gmail message ID (per-inbox id)."""
    db = SessionLocal()
    try:
        delivery = db.scalar(
            select(EmailDelivery).where(
                EmailDelivery.gmail_message_id == gmail_message_id
            )
        )
        return serialize(delivery.email) if delivery else None
    finally:
        db.close()

@mcp.tool()
def list_emails(limit: int | None = None) -> list[dict]:
    """List all emails, most recent first. Omit limit for everything."""
    stmt = select(Email).order_by(Email.received_at.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return query(stmt)

@mcp.tool()
def list_emails_by_category(
    category: str, limit: int | None = None
) -> list[dict]:
    """category: primary | social | promotions | updates | forums"""
    stmt = (
        select(Email)
        .where(Email.category == category.lower())
        .order_by(Email.received_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return query(stmt)

@mcp.tool()
def get_thread(thread_id: str) -> list[dict]:
    return query(
        select(Email).where(Email.thread_id == thread_id).order_by(Email.received_at)
    )

@mcp.tool()
def search_by_sender(sender: str, limit: int | None = None) -> list[dict]:
    stmt = (
        select(Email)
        .where(Email.sender_email.ilike(f"%{sender}%"))
        .order_by(Email.received_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return query(stmt)

@mcp.tool()
def search_by_subject(subject: str, limit: int | None = None) -> list[dict]:
    stmt = (
        select(Email)
        .where(Email.subject.ilike(f"%{subject}%"))
        .order_by(Email.received_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return query(stmt)

@mcp.tool()
def search_by_date(start_iso: str, end_iso: str, limit: int | None = None) -> list[dict]:
    stmt = (
        select(Email)
        .where(
            Email.received_at >= datetime.fromisoformat(start_iso),
            Email.received_at <= datetime.fromisoformat(end_iso),
        )
        .order_by(Email.received_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return query(stmt)