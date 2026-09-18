from __future__ import annotations

import base64
import logging
from typing import Any


from sqlalchemy import or_, select

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from app.config import settings
from app.database import SessionLocal
from app.models.email import Email, EmailAttachment


# ============================================================
# CONFIG
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "Gmail Email MCP",
)


# ============================================================
# HELPERS
# ============================================================

def group_recipients(recipients: Any) -> dict[str, list[dict[str, Any]]]:
    """
    Group email recipients into to / cc / bcc.
    """
    result = {
        "to": [],
        "cc": [],
        "bcc": [],
    }

    if not recipients:
        return result

    if isinstance(recipients, dict):
        for key in result:
            value = recipients.get(key, [])
            if isinstance(value, list):
                result[key] = value
        return result

    if isinstance(recipients, list):
        for recipient in recipients:
            if not isinstance(recipient, dict):
                continue

            recipient_type = str(
                recipient.get("type", "to")
            ).lower()

            if recipient_type in result:
                result[recipient_type].append(recipient)

    return result


def serialize_email(email: Email) -> dict[str, Any]:
    """
    Convert Email ORM object into a clean MCP response.
    """

    recipients = group_recipients(email.recipients)

    return {
        "id": email.id,
        "rfc_message_id": email.rfc_message_id,
        "thread_id": email.thread_id,

        "sender": {
            "name": email.sender_name,
            "email": email.sender_email,
        },

        "recipients": recipients,

        "subject": email.subject,
        "body": email.body_text,

        "received_at": (
            email.received_at.isoformat()
            if email.received_at
            else None
        ),

        "labels": email.labels or [],
        "category": email.category,

        "has_attachments": bool(email.has_attachments),

        "attachments": email.attachments or [],

        "created_at": (
            email.created_at.isoformat()
            if getattr(email, "created_at", None)
            else None
        ),
    }


def serialize_attachment(
    attachment: EmailAttachment,
    include_content: bool = False,
) -> dict[str, Any]:
    """
    Serialize an EmailAttachment.
    """

    result = {
        "id": attachment.id,
        "email_id": attachment.email_id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size": attachment.size,
        "content_stored": attachment.content is not None,
    }

    if include_content and attachment.content is not None:
        result["content_base64"] = base64.b64encode(
            attachment.content
        ).decode("utf-8")

    return result


def attachment_download_url(attachment_id: int) -> str:
    """
    Generate public attachment download URL.
    """

    base_url = str(settings.public_base_url).rstrip("/")

    return (
        f"{base_url}/api/attachments/"
        f"{attachment_id}/download"
    )



def attachment_to_content_blocks(attachment):
    import mimetypes

    if not attachment.content:
        return [
            TextContent(type="text", text=f"Attachment content is not stored: {attachment.filename}")
        ]

    mime_type = attachment.mime_type or ""

    # Fallback for attachments already stored with a blank MIME type
    # (from before Bug #1 was fixed) — guess from the filename instead.
    if not mime_type:
        guessed, _ = mimetypes.guess_type(attachment.filename or "")
        mime_type = guessed or "application/octet-stream"

    download_url = attachment_download_url(attachment.id)

    if mime_type.startswith("image/"):
        encoded = base64.b64encode(attachment.content).decode("utf-8")
        return [
            TextContent(
                type="text",
                text=(
                    f"Image: {attachment.filename}\n"
                    f"MIME type: {mime_type}\n"
                    f"Size: {attachment.size} bytes\n"
                    f"Download: {download_url}"
                )
            ),
            ImageContent(
                type="image",
                data=encoded,
                mimeType=mime_type,   # ✅ was "mime_type" — wrong ImageContent field
            ),
        ]

    return [
        TextContent(
            type="text",
            text=f"Attachment: {attachment.filename}\n"
                 f"MIME type: {mime_type}\n"
                 f"Download: {download_url}"
        )
    ]
# ============================================================
# TOOL 2
# GET EMAIL
# ============================================================

@mcp.tool()
def get_email(
    email_id: int,
) -> dict[str, Any]:
    """
    Get complete details of one email by database ID.
    """

    with SessionLocal() as database:

        email = database.get(
            Email,
            email_id,
        )

        if not email:
            return {
                "error": f"Email {email_id} not found."
            }

        return serialize_email(email)


# ============================================================
# TOOL 3
# LIST EMAILS
# ============================================================

@mcp.tool()
def list_emails(
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """
    List recent emails with pagination.
    """

    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    with SessionLocal() as database:

        stmt = (
            select(Email)
            .order_by(
                Email.received_at.desc()
            )
            .offset(offset)
            .limit(limit)
        )

        emails = database.execute(stmt).scalars().all()

        return [
            serialize_email(email)
            for email in emails
        ]


# ============================================================
# TOOL 4
# GET THREAD
# ============================================================

@mcp.tool()
def get_thread(
    thread_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Get all emails belonging to a Gmail thread.
    """

    thread_id = (thread_id or "").strip()

    if not thread_id:
        return []

    limit = max(1, min(limit, 200))

    with SessionLocal() as database:

        stmt = (
            select(Email)
            .where(
                Email.thread_id == thread_id
            )
            .order_by(
                Email.received_at.asc()
            )
            .limit(limit)
        )

        emails = database.execute(stmt).scalars().all()

        return [
            serialize_email(email)
            for email in emails
        ]


# ============================================================
# TOOL 5
# SEARCH EMAILS WITH ATTACHMENTS
# ============================================================

@mcp.tool()
def search_emails_with_attachments(
    query: str = "",
    attachment_type: str = "any",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Search emails that have attachments.
    """

    limit = max(1, min(limit, 100))
    query = (query or "").strip()
    attachment_type = (attachment_type or "any").lower().strip()

    with SessionLocal() as database:

        # Step 1: find matching email IDs only (Email.id is a plain
        # integer column, so DISTINCT works fine here — no JSON involved).
        id_stmt = (
            select(Email.id)
            .join(EmailAttachment, EmailAttachment.email_id == Email.id)
        )

        if query:
            pattern = f"%{query}%"
            id_stmt = id_stmt.where(
                or_(
                    Email.subject.ilike(pattern),
                    Email.body_text.ilike(pattern),
                    Email.sender_email.ilike(pattern),
                    Email.sender_name.ilike(pattern),
                    Email.thread_id.ilike(pattern),
                    Email.rfc_message_id.ilike(pattern),
                    EmailAttachment.filename.ilike(pattern),
                )
            )

        if attachment_type != "any":
            condition = attachment_type_condition(attachment_type)
            if condition is not None:
                id_stmt = id_stmt.where(condition)

        email_ids = [row[0] for row in database.execute(id_stmt.distinct()).all()]

        if not email_ids:
            return []

        # Step 2: fetch full Email rows (including the JSON columns) —
        # no DISTINCT needed here, so the JSON columns are no problem.
        stmt = (
            select(Email)
            .where(Email.id.in_(email_ids))
            .order_by(Email.received_at.desc())
            .limit(limit)
        )

        emails = database.execute(stmt).scalars().all()

        return [serialize_email(email) for email in emails]
# ============================================================
# TOOL 6
# LIST ATTACHMENTS
# ============================================================

@mcp.tool()
def list_attachments(
    email_id: int,
) -> list[dict[str, Any]]:
    """
    List all PostgreSQL-stored attachments for an email.
    """

    with SessionLocal() as database:

        email = database.get(
            Email,
            email_id,
        )

        if not email:
            return []

        stmt = (
            select(EmailAttachment)
            .where(
                EmailAttachment.email_id == email_id
            )
            .order_by(
                EmailAttachment.id.asc()
            )
        )

        attachments = (
            database.execute(stmt)
            .scalars()
            .all()
        )

        return [
            serialize_attachment(attachment)
            for attachment in attachments
        ]


# ============================================================
# TOOL 7
# GET ATTACHMENT CONTENT
# ============================================================

@mcp.tool()
def get_attachment_content(attachment_id: int):
    db = SessionLocal()

    try:
        attachment = (
            db.query(EmailAttachment)
            .filter(EmailAttachment.id == attachment_id)
            .first()
        )

        if not attachment:
            return "Attachment not found"

        return attachment_to_content_blocks(attachment)

    finally:
        db.close()

# ============================================================
# TOOL 8
# EXTRACT ATTACHMENT TEXT
# ============================================================
@mcp.tool()
def extract_attachment_text(attachment_id: int) -> dict[str, Any]:
    from app.services.attachment_service import extract_text_from_attachment

    with SessionLocal() as database:
        attachment = database.get(EmailAttachment, attachment_id)

        if not attachment:
            return {"error": f"Attachment {attachment_id} not found."}

        if not attachment.content:
            return {
                "error": "Attachment content is not stored in PostgreSQL.",
                "attachment": serialize_attachment(attachment),
            }

        try:
            text = extract_text_from_attachment(database, attachment_id)  # ✅ correct signature

            if text is None:
                return {
                    "attachment_id": attachment.id,
                    "filename": attachment.filename,
                    "mime_type": attachment.mime_type,
                    "text": None,
                    "note": "No text could be extracted (unsupported type or empty content).",
                }

            return {
                "attachment_id": attachment.id,
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "text": text,
            }

        except Exception as exc:
            logger.exception("Attachment text extraction failed")
            return {"error": str(exc)}


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    logger.info(
        "Starting Gmail Email MCP server..."
    )

    mcp.run(
        transport="streamable-http"
    )