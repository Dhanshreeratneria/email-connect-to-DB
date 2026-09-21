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
from app.services import auth0


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
        "unknown": [],
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
            if isinstance(recipient, str):
                result["unknown"].append({"email": recipient})
                continue
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


def serialize(email: Email) -> dict[str, Any]:
    """Backward-compatible serializer for older MCP clients."""
    result = serialize_email(email)
    result["recipient_count"] = sum(
        len(values) for values in result["recipients"].values()
    )
    return result


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
        "download_url": attachment_download_url(attachment.id),
        "view_url": attachment_view_url(attachment.id),
        "is_displayable": bool(
            attachment.content and (
                (attachment.mime_type or "").lower().startswith("image/")
                or (attachment.mime_type or "").lower() == "application/pdf"
            )
        ),
    }

    if include_content and attachment.content is not None:
        result["content_base64"] = base64.b64encode(
            attachment.content
        ).decode("utf-8")

    return result


def attachment_download_url(attachment_id: int) -> str:
    """
    Generate public attachment download URL.

    BUG FIX: this used to build ".../api/attachments/{id}/download", but
    the download route is mounted at the app root with no "/api" prefix
    (see app/api/attachments.py / app/api/emails.py — both use
    `router = APIRouter()` with no prefix, and main.py includes them with
    no prefix either). The old URL 404'd every single time, which is why
    "Failed to fetch: .../api/attachments/49/download" was happening even
    though attachment 49's bytes were actually stored fine.
    """

    base_url = str(settings.public_base_url).rstrip("/")

    return (
        f"{base_url}/attachments/"
        f"{attachment_id}/download"
    )


def attachment_view_url(attachment_id: int) -> str:
    return f"{str(settings.public_base_url).rstrip('/')}/attachments/{attachment_id}/view"


def attachment_type_condition(attachment_type: str):
    """
    Build a filter condition on EmailAttachment.mime_type for a given
    category name.

    BUG FIX: search_emails_with_attachments called this function for any
    attachment_type other than "any", but it was never defined anywhere
    in this file — every call with attachment_type="image", "pdf", etc.
    raised NameError. Only attachment_type="any" (which skips this call)
    happened to work before.
    """
    attachment_type = (attachment_type or "").lower().strip()

    if attachment_type in ("image", "images"):
        return EmailAttachment.mime_type.ilike("image/%")
    if attachment_type == "pdf":
        return EmailAttachment.mime_type == "application/pdf"
    if attachment_type in ("document", "doc", "docx"):
        return or_(
            EmailAttachment.mime_type.ilike("%wordprocessingml%"),
            EmailAttachment.mime_type == "application/msword",
        )
    if attachment_type in ("spreadsheet", "sheet", "xlsx", "csv"):
        return or_(
            EmailAttachment.mime_type.ilike("%spreadsheetml%"),
            EmailAttachment.mime_type == "application/vnd.ms-excel",
            EmailAttachment.mime_type == "text/csv",
        )
    if attachment_type in ("archive", "zip"):
        return or_(
            EmailAttachment.mime_type == "application/zip",
            EmailAttachment.mime_type.ilike("%compressed%"),
        )

    # Unrecognized category: don't filter anything out rather than
    # silently returning zero results.
    return None



def attachment_to_content_blocks(database, attachment):
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
    view_url = attachment_view_url(attachment.id)

    if mime_type.startswith("image/"):
        encoded = base64.b64encode(attachment.content).decode("utf-8")
        image_content_args = {
            "type": "image",
            "data": encoded,
        }
        if "mime_type" in ImageContent.model_fields:
            image_content = ImageContent(
                **image_content_args,
                mime_type=mime_type,
            )
        else:
            image_content = ImageContent(
                **image_content_args,
                mimeType=mime_type,
            )
        return [
            TextContent(
                type="text",
                text=(
                    f"Image: {attachment.filename}\n"
                    f"MIME type: {mime_type}\n"
                    f"Size: {attachment.size} bytes\n"
                    f"View: {view_url}\nDownload: {download_url}"
                )
            ),
            image_content,
        ]

    if mime_type == "application/pdf":
        from app.services.attachment_service import extract_text_from_attachment

        blocks = [
            TextContent(
                type="text",
                text=(
                    f"PDF: {attachment.filename}\n"
                    f"View: {view_url}\nDownload: {download_url}"
                ),
            )
        ]
        extracted_text = extract_text_from_attachment(database, attachment.id)
        if extracted_text:
            blocks.append(
                TextContent(type="text", text=f"Extracted PDF text:\n{extracted_text}")
            )
        try:
            import fitz

            document = fitz.open(stream=attachment.content, filetype="pdf")
            for page_number in range(min(5, document.page_count)):
                page = document[page_number]
                image = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                blocks.append(
                    ImageContent(
                        type="image",
                        data=base64.b64encode(image.tobytes("png")).decode("utf-8"),
                        mimeType="image/png",
                    )
                )
            document.close()
        except Exception:
            logger.exception("Failed to render PDF attachment %s", attachment.id)
        return blocks

    from app.services.attachment_service import extract_text_from_attachment

    extracted_text = extract_text_from_attachment(database, attachment.id)
    if extracted_text:
        return [
            TextContent(
                type="text",
                text=(
                    f"Attachment: {attachment.filename}\n"
                    f"MIME type: {mime_type}\n"
                    f"View: {view_url}\nDownload: {download_url}\n\n"
                    f"Extracted text:\n{extracted_text}"
                ),
            )
        ]

    return [
        TextContent(
            type="text",
            text=f"Attachment: {attachment.filename}\n"
                 f"MIME type: {mime_type}\n"
                 f"View: {view_url}\n"
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

    auth0.require_scope("read:emails")
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
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """
    List recent emails with pagination. Omit limit to return all emails.
    """

    auth0.require_scope("read:emails")
    offset = max(0, offset)

    with SessionLocal() as database:

        stmt = (
            select(Email)
            .order_by(
                Email.received_at.desc()
            )
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(max(1, limit))

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
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """
    Get all emails belonging to a Gmail thread. Omit limit to return the
    whole thread.
    """

    auth0.require_scope("read:emails")
    thread_id = (thread_id or "").strip()

    if not thread_id:
        return []

    with SessionLocal() as database:

        stmt = (
            select(Email)
            .where(
                Email.thread_id == thread_id
            )
            .order_by(
                Email.received_at.asc()
            )
        )
        if limit is not None:
            stmt = stmt.limit(max(1, limit))

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
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """
    Search emails that have attachments. Omit limit to return all matches.
    """

    auth0.require_scope("read:emails")
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
        )
        if limit is not None:
            stmt = stmt.limit(max(1, limit))

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

    auth0.require_scope("read:attachments")
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

@mcp.tool(structured_output=False)
def get_attachment_content(attachment_id: int):
    auth0.require_scope("read:attachments")
    db = SessionLocal()

    try:
        attachment = (
            db.query(EmailAttachment)
            .filter(EmailAttachment.id == attachment_id)
            .first()
        )

        if not attachment:
            return "Attachment not found"

        return attachment_to_content_blocks(db, attachment)

    finally:
        db.close()

# ============================================================
# TOOL 8
# EXTRACT ATTACHMENT TEXT
# ============================================================
@mcp.tool()
def extract_attachment_text(attachment_id: int) -> dict[str, Any]:
    from app.services.attachment_service import extract_text_from_attachment

    auth0.require_scope("read:attachments")
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
            text = extract_text_from_attachment(database, attachment_id)  # correct signature

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