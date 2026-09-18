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
from app.services.attachment_service import (
    attachment_category,
    extract_text_from_attachment,
    render_pdf_pages,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("Gmail Email MCP")


def group_recipients(recipients: Any) -> dict[str, list[dict[str, Any]]]:
    result = {"to": [], "cc": [], "bcc": []}
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
            if isinstance(recipient, dict):
                kind = str(recipient.get("type", "to")).lower()
                if kind in result:
                    result[kind].append(recipient)
    return result


def attachment_download_url(attachment_id: int, inline: bool = False) -> str:
    base = str(settings.public_base_url).rstrip("/")
    action = "view" if inline else "download"
    return f"{base}/api/attachments/{attachment_id}/{action}"


def serialize_attachment(att: EmailAttachment) -> dict[str, Any]:
    return {
        "id": att.id,
        "email_id": att.email_id,
        "filename": att.filename,
        "mime_type": att.mime_type,
        "size": att.size,
        "category": attachment_category(att.mime_type, att.filename),
        "content_stored": att.content is not None,
        "download_url": attachment_download_url(att.id),
        "view_url": attachment_download_url(att.id, inline=True),
    }


def serialize_email(email: Email) -> dict[str, Any]:
    return {
        "id": email.id,
        "rfc_message_id": email.rfc_message_id,
        "thread_id": email.thread_id,
        "sender": {
            "name": email.sender_name,
            "email": email.sender_email,
        },
        "recipients": group_recipients(email.recipients),
        "subject": email.subject,
        "body": email.body_text,
        "received_at": email.received_at.isoformat() if email.received_at else None,
        "labels": email.labels or [],
        "category": email.category,
        "has_attachments": bool(email.has_attachments),
        "attachments": email.attachments or [],
        "created_at": email.created_at.isoformat() if email.created_at else None,
    }


def attachment_content_blocks(
    att: EmailAttachment,
    max_pdf_pages: int = 5,
) -> list[TextContent | ImageContent]:
    """
    Return an attachment in the most useful MCP representation.

    Images are sent as ImageContent.
    PDFs are rendered to page images (up to max_pdf_pages) and also
    have their text extracted.
    Other supported documents have extracted text.
    Every file gets a browser download URL.
    """
    mime = (att.mime_type or "application/octet-stream").lower()
    name = att.filename or "attachment"
    blocks: list[TextContent | ImageContent] = []

    if not att.content:
        return [
            TextContent(
                type="text",
                text=(
                    f"Attachment: {name}\n"
                    f"Type: {mime}\n"
                    "Content is not stored in PostgreSQL.\n"
                    f"Download: {attachment_download_url(att.id)}"
                ),
            )
        ]

    blocks.append(
        TextContent(
            type="text",
            text=(
                f"Attachment: {name}\n"
                f"Type: {mime}\n"
                f"Size: {att.size or len(att.content)} bytes\n"
                f"Download: {attachment_download_url(att.id)}\n"
                f"View: {attachment_download_url(att.id, inline=True)}"
            ),
        )
    )

    # Images: native MCP image content.
    if mime.startswith("image/"):
        encoded = base64.b64encode(att.content).decode("ascii")
        blocks.append(
            ImageContent(
                type="image",
                data=encoded,
                mime_type=mime,
            )
        )
        return blocks

    # PDF: render pages to images so Claude can visually inspect them,
    # and also provide extracted text for searchable content.
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        pages = render_pdf_pages(att.content, max_pages=max_pdf_pages)
        for page_number, png_bytes in pages:
            blocks.append(
                TextContent(
                    type="text",
                    text=f"PDF page {page_number}",
                )
            )
            blocks.append(
                ImageContent(
                    type="image",
                    data=base64.b64encode(png_bytes).decode("ascii"),
                    mime_type="image/png",
                )
            )

    # Documents/spreadsheets/presentations/text: provide readable text.
    text = extract_text_from_attachment(att)
    if text:
        # Keep one MCP response reasonably sized.
        max_chars = 50000
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[Text truncated; use the download URL for the full file.]"
        blocks.append(
            TextContent(
                type="text",
                text=f"Extracted text from {name}:\n\n{text}",
            )
        )

    return blocks


def attachment_type_condition(attachment_type: str):
    kind = attachment_type.lower().strip()
    if kind == "image":
        return EmailAttachment.mime_type.ilike("image/%")
    if kind == "pdf":
        return EmailAttachment.mime_type.ilike("application/pdf")
    if kind == "document":
        return or_(
            EmailAttachment.mime_type.ilike("%word%"),
            EmailAttachment.mime_type.ilike("%wordprocessingml%"),
            EmailAttachment.mime_type.ilike("text/plain"),
            EmailAttachment.mime_type.ilike("application/rtf"),
        )
    if kind == "spreadsheet":
        return or_(
            EmailAttachment.mime_type.ilike("%excel%"),
            EmailAttachment.mime_type.ilike("%spreadsheetml%"),
            EmailAttachment.mime_type.ilike("text/csv"),
        )
    if kind == "presentation":
        return or_(
            EmailAttachment.mime_type.ilike("%presentation%"),
            EmailAttachment.mime_type.ilike("%powerpoint%"),
        )
    if kind == "archive":
        return or_(
            EmailAttachment.mime_type.ilike("%zip%"),
            EmailAttachment.mime_type.ilike("%rar%"),
            EmailAttachment.mime_type.ilike("%7z%"),
            EmailAttachment.mime_type.ilike("%tar%"),
            EmailAttachment.mime_type.ilike("%gzip%"),
        )
    return None


@mcp.tool()
def search_emails(query: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """Search Gmail emails by sender, recipient, subject, body, thread ID, or message ID."""
    limit = max(1, min(limit, 100))
    query = (query or "").strip()

    with SessionLocal() as db:
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
        emails = db.execute(
            stmt.order_by(Email.received_at.desc()).limit(limit)
        ).scalars().all()
        return [serialize_email(e) for e in emails]


@mcp.tool()
def get_email(email_id: int) -> dict[str, Any]:
    """Get one complete email by database ID."""
    with SessionLocal() as db:
        email = db.get(Email, email_id)
        return serialize_email(email) if email else {"error": f"Email {email_id} not found."}


@mcp.tool()
def list_emails(limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    """List recent emails with pagination."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    with SessionLocal() as db:
        emails = db.execute(
            select(Email)
            .order_by(Email.received_at.desc())
            .offset(offset)
            .limit(limit)
        ).scalars().all()
        return [serialize_email(e) for e in emails]


@mcp.tool()
def get_thread(thread_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """Get emails belonging to a Gmail thread."""
    thread_id = (thread_id or "").strip()
    if not thread_id:
        return []
    limit = max(1, min(limit, 200))
    with SessionLocal() as db:
        emails = db.execute(
            select(Email)
            .where(Email.thread_id == thread_id)
            .order_by(Email.received_at.asc())
            .limit(limit)
        ).scalars().all()
        return [serialize_email(e) for e in emails]


@mcp.tool()
def search_emails_with_attachments(
    query: str = "",
    attachment_type: str = "any",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Search emails with attachments actually stored in PostgreSQL.
    Types: any, image, pdf, document, spreadsheet, presentation, archive.
    """
    limit = max(1, min(limit, 100))
    query = (query or "").strip()
    attachment_type = (attachment_type or "any").lower().strip()

    with SessionLocal() as db:
        stmt = select(Email).join(
            EmailAttachment,
            EmailAttachment.email_id == Email.id,
        )

        if query:
            pattern = f"%{query}%"
            stmt = stmt.where(
                or_(
                    Email.subject.ilike(pattern),
                    Email.body_text.ilike(pattern),
                    Email.sender_email.ilike(pattern),
                    Email.sender_name.ilike(pattern),
                    Email.thread_id.ilike(pattern),
                    EmailAttachment.filename.ilike(pattern),
                )
            )

        if attachment_type != "any":
            condition = attachment_type_condition(attachment_type)
            if condition is not None:
                stmt = stmt.where(condition)

        emails = db.execute(
            stmt.distinct()
            .order_by(Email.received_at.desc())
            .limit(limit)
        ).scalars().all()
        return [serialize_email(e) for e in emails]


@mcp.tool()
def list_attachments(email_id: int) -> list[dict[str, Any]]:
    """List all stored attachments for an email, including download/view URLs."""
    with SessionLocal() as db:
        attachments = db.execute(
            select(EmailAttachment)
            .where(EmailAttachment.email_id == email_id)
            .order_by(EmailAttachment.id.asc())
        ).scalars().all()
        return [serialize_attachment(a) for a in attachments]


@mcp.tool(structured_output=False)
def get_attachment_content(
    attachment_id: int,
    max_pdf_pages: int = 5,
) -> list[TextContent | ImageContent]:
    """
    Retrieve an attachment.

    Images are returned as native MCP ImageContent.
    PDFs are rendered as page images (up to max_pdf_pages) and text is extracted.
    DOCX/XLSX/PPTX/CSV/TXT/RTF content is extracted as text.
    All files include download/view URLs.
    """
    max_pdf_pages = max(1, min(max_pdf_pages, 10))
    with SessionLocal() as db:
        attachment = db.get(EmailAttachment, attachment_id)
        if not attachment:
            return [TextContent(type="text", text=f"Attachment {attachment_id} not found.")]
        return attachment_content_blocks(attachment, max_pdf_pages=max_pdf_pages)


@mcp.tool()
def extract_attachment_text(attachment_id: int) -> dict[str, Any]:
    """Extract readable text from a stored PDF, DOCX, XLSX, PPTX, CSV, TXT, or RTF attachment."""
    with SessionLocal() as db:
        attachment = db.get(EmailAttachment, attachment_id)
        if not attachment:
            return {"error": f"Attachment {attachment_id} not found."}
        if not attachment.content:
            return {
                "error": "Attachment content is not stored in PostgreSQL.",
                "attachment": serialize_attachment(attachment),
            }
        text = extract_text_from_attachment(attachment)
        return {
            "attachment_id": attachment.id,
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "text": text,
            "download_url": attachment_download_url(attachment.id),
        }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
