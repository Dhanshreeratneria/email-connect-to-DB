from datetime import datetime
from sqlalchemy import or_, select
from mcp.server.fastmcp import FastMCP
from app.database import SessionLocal
from app.models.email import Email

mcp = FastMCP(
    "Gmail Email Search",
    instructions="Read-only email search. Never expose OAuth tokens or secrets."
)

def serialize(e: Email):
    return {
        "message_id": e.message_id,
        "thread_id": e.thread_id,
        "sender_name": e.sender_name,
        "sender_email": e.sender_email,
        "recipients": e.recipients,
        "subject": e.subject,
        "body_text": e.body_text,
        "received_at": e.received_at.isoformat(),
        "labels": e.labels,
        "has_attachments": e.has_attachments,
        "attachments": e.attachments,
    }

def query(stmt):
    db = SessionLocal()
    try:
        return [serialize(e) for e in db.scalars(stmt).all()]
    finally:
        db.close()

@mcp.tool()
def search_emails(query_text: str, limit: int = 20) -> list[dict]:
    """Search sender, subject, and text body."""
    q = f"%{query_text}%"
    return query(
        select(Email)
        .where(
            or_(
                Email.sender_email.ilike(q),
                Email.subject.ilike(q),
                Email.body_text.ilike(q),
            )
        )
        .order_by(Email.received_at.desc())
        .limit(min(limit, 100))
    )

@mcp.tool()
def get_email(message_id: str) -> dict | None:
    """Get one stored email by Gmail message ID."""
    rows = query(select(Email).where(Email.message_id == message_id))
    return rows[0] if rows else None

@mcp.tool()
def list_emails(limit: int = 20) -> list[dict]:
    return query(
        select(Email).order_by(Email.received_at.desc()).limit(min(limit, 100))
    )

@mcp.tool()
def get_thread(thread_id: str) -> list[dict]:
    return query(
        select(Email).where(Email.thread_id == thread_id).order_by(Email.received_at)
    )

@mcp.tool()
def search_by_sender(sender: str, limit: int = 20) -> list[dict]:
    return query(
        select(Email)
        .where(Email.sender_email.ilike(f"%{sender}%"))
        .order_by(Email.received_at.desc())
        .limit(min(limit, 100))
    )

@mcp.tool()
def search_by_subject(subject: str, limit: int = 20) -> list[dict]:
    return query(
        select(Email)
        .where(Email.subject.ilike(f"%{subject}%"))
        .order_by(Email.received_at.desc())
        .limit(min(limit, 100))
    )

@mcp.tool()
def search_by_date(start_iso: str, end_iso: str, limit: int = 50) -> list[dict]:
    return query(
        select(Email)
        .where(
            Email.received_at >= datetime.fromisoformat(start_iso),
            Email.received_at <= datetime.fromisoformat(end_iso),
        )
        .order_by(Email.received_at.desc())
        .limit(min(limit, 100))
    )