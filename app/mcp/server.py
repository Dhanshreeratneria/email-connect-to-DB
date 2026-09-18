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
    if not attachment.content:
        return [
            TextContent(
                type="text",
                text=f"Attachment content is not stored: {attachment.filename}"
            )
        ]

    mime_type = attachment.mime_type or "application/octet-stream"

    if mime_type.startswith("image/"):
        encoded = base64.b64encode(attachment.content).decode("utf-8")

        return [
            TextContent(
                type="text",
                text=f"Image: {attachment.filename}"
            ),
            ImageContent(
                type="image",
                data=encoded,
                mime_type=mime_type,
            ),
        ]

    return [
        TextContent(
            type="text",
            text=f"Attachment: {attachment.filename}\n"
                 f"MIME type: {mime_type}"
        )
    ]
def attachment_type_condition(
    attachment_type: str,
):
    """
    Build SQL condition for attachment category.
    """

    attachment_type = attachment_type.lower().strip()

    if attachment_type == "image":
        return EmailAttachment.mime_type.ilike("image/%")

    if attachment_type == "pdf":
        return EmailAttachment.mime_type.ilike(
            "application/pdf"
        )

    if attachment_type == "document":
        return or_(
            EmailAttachment.mime_type.ilike(
                "application/msword%"
            ),
            EmailAttachment.mime_type.ilike(
                "application/vnd.openxmlformats-officedocument.wordprocessingml%"
            ),
            EmailAttachment.mime_type.ilike(
                "text/plain"
            ),
            EmailAttachment.mime_type.ilike(
                "application/rtf"
            ),
        )

    if attachment_type == "spreadsheet":
        return or_(
            EmailAttachment.mime_type.ilike(
                "text/csv"
            ),
            EmailAttachment.mime_type.ilike(
                "application/vnd.ms-excel%"
            ),
            EmailAttachment.mime_type.ilike(
                "application/vnd.openxmlformats-officedocument.spreadsheetml%"
            ),
        )

    if attachment_type == "archive":
        return or_(
            EmailAttachment.mime_type.ilike(
                "application/zip"
            ),
            EmailAttachment.mime_type.ilike(
                "application/x-rar%"
            ),
            EmailAttachment.mime_type.ilike(
                "application/x-7z%"
            ),
            EmailAttachment.mime_type.ilike(
                "application/gzip"
            ),
            EmailAttachment.mime_type.ilike(
                "application/x-tar"
            ),
        )

    return None


# ============================================================
# TOOL 1
# SEARCH EMAILS
# ============================================================

@mcp.tool()
def search_emails(
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Search emails by sender, recipient, subject, body,
    thread ID, or message ID.

    Use this as the main email search tool.
    """

    limit = max(1, min(limit, 100))
    query = (query or "").strip()

    with SessionLocal() as database:

        stmt = select(Email)

        if query:
            pattern = f"%{query}%"

            stmt = stmt.where(
                or_(
                    Email.subject.ilike(pattern),
                    Email.body_text.ilike(pattern),
                    Email.sender_email.ilike(pattern),
                    Email.sender_name.ilike(pattern),
                    Email.thread_id.ilike(pattern),
                    Email.rfc_message_id.ilike(pattern),
                )
            )

        stmt = stmt.order_by(
            Email.received_at.desc()
        ).limit(limit)

        emails = database.execute(stmt).scalars().all()

        return [
            serialize_email(email)
            for email in emails
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

    attachment_type:
      - any
      - image
      - pdf
      - document
      - spreadsheet
      - archive

    This checks the actual EmailAttachment table,
    meaning the attachment has been stored in PostgreSQL.
    """

    limit = max(1, min(limit, 100))
    query = (query or "").strip()
    attachment_type = (
        attachment_type or "any"
    ).lower().strip()

    with SessionLocal() as database:

        stmt = (
            select(Email)
            .join(
                EmailAttachment,
                EmailAttachment.email_id == Email.id,
            )
        )

        # ----------------------------------------------------
        # EMAIL SEARCH
        # ----------------------------------------------------

        if query:

            pattern = f"%{query}%"

            stmt = stmt.where(
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

        # ----------------------------------------------------
        # ATTACHMENT TYPE
        # ----------------------------------------------------

        if attachment_type != "any":

            condition = attachment_type_condition(
                attachment_type
            )

            if condition is not None:
                stmt = stmt.where(condition)

        # ----------------------------------------------------
        # DISTINCT EMAILS
        # ----------------------------------------------------

        stmt = (
            stmt
            .distinct()
            .order_by(
                Email.received_at.desc()
            )
            .limit(limit)
        )

        emails = database.execute(stmt).scalars().all()

        return [
            serialize_email(email)
            for email in emails
        ]


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
def extract_attachment_text(
    attachment_id: int,
) -> dict[str, Any]:
    """
    Extract text from supported document attachments.

    The actual extraction implementation should be provided
    by the attachment service.
    """

    from app.services.attachment_service import (
        extract_text_from_attachment,
    )

    with SessionLocal() as database:

        attachment = database.get(
            EmailAttachment,
            attachment_id,
        )

        if not attachment:
            return {
                "error": (
                    f"Attachment "
                    f"{attachment_id} not found."
                )
            }

        if not attachment.content:
            return {
                "error": (
                    "Attachment content is not "
                    "stored in PostgreSQL."
                ),
                "attachment": serialize_attachment(
                    attachment
                ),
            }

        try:

            text = extract_text_from_attachment(
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                content=attachment.content,
            )

            return {
                "attachment_id": attachment.id,
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "text": text,
            }

        except Exception as exc:

            logger.exception(
                "Attachment text extraction failed"
            )

            return {
                "error": (
                    "Failed to extract attachment text."
                ),
                "details": str(exc),
                "attachment_id": attachment.id,
                "filename": attachment.filename,
            }


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