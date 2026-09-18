"""
MCP Server for Gmail Email Search with attachment support.

Images are sent as real inline MCP ImageContent blocks (actual base64
image bytes + mimeType, the protocol's native way to return an image) so
Claude renders the picture itself — not just a markdown link pointing at
an HTTPS URL, which depends on the client choosing to fetch and render
an arbitrary external image link and was producing descriptions instead
of the actual picture. Every attachment (image or not) also always gets
a real, directly-fetchable HTTPS download link.

- get_attachment_content: Returns an attachment inline (image) or a
  download link (everything else — PDF, docx, xlsx, zip, ...)
- extract_attachment_text: Extracts text from PDFs, docs, spreadsheets, etc.
- get_email_with_attachments: Returns email with attachments rendered inline
- list_attachments: List attachments for an email with displayable status
- sync_missing_attachments: Backfills attachment bytes for emails that
  were synced before their content was downloaded

All attachments stored in PostgreSQL are accessible to Claude.
"""

import base64
from datetime import datetime

from sqlalchemy import or_, select
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from app.config import settings
from app.database import SessionLocal
from app.models.email import Email, EmailDelivery, EmailAttachment
from app.services.sync_service import backfill_missing_attachments
from app.services.attachment_service import (
    get_attachment_content_base64,
    get_email_attachments_with_content,
    extract_text_from_attachment,
    is_displayable_in_claude,
)

# Above this size, an image is sent as a download link only instead of an
# inline ImageContent block — large base64 payloads in a single MCP tool
# result can blow past transport/context limits.
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB

INLINE_IMAGE_MIME_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
}

mcp = FastMCP(
    "Gmail Email Search",
    instructions=(
        "Read-only email search with attachment support. "
        "Can retrieve, display, and extract text from attachments. "
        "Never expose OAuth tokens or secrets."
    )
)


def group_recipients(recipients: list) -> dict[str, list[dict[str, str | None]]]:
    """
    Groups the stored recipients list by delivery type (\"to\"/\"cc\"/\"bcc\")
    so callers get a ready-to-use breakdown instead of a flat list they
    have to filter themselves.

    Tolerates rows synced before recipients carried a \"type\" (older data
    stored as plain email strings) by bucketing those under \"unknown\"
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
        "attachments": e.attachments,
        # Count of stored attachments with actual content
        "stored_attachments_count": len(e.stored_attachments) if e.stored_attachments else 0,
    }


def serialize_attachment(att: EmailAttachment, include_content: bool = False):
    """
    Serializes an EmailAttachment, optionally including base64 content.
    """
    result = {
        "id": att.id,
        "filename": att.filename,
        "mime_type": att.mime_type,
        "size": att.size,
        "gmail_attachment_id": att.gmail_attachment_id,
        "created_at": att.created_at.isoformat() if att.created_at else None,
        "is_displayable": is_displayable_in_claude(att.mime_type),
        "has_content": att.content is not None,
    }
    
    if include_content and att.content:
        import base64
        result["content_base64"] = base64.b64encode(att.content).decode("utf-8")
    
    return result


def attachment_download_url(att: EmailAttachment) -> str:
    """Public, directly-fetchable HTTPS URL for one stored attachment."""
    base = settings.public_base_url.rstrip("/")
    return f"{base}/attachments/{att.id}/download"


def attachment_to_content_blocks(att: EmailAttachment) -> list:
    """
    Builds the actual displayable/downloadable content for one stored
    attachment.

    - Images (JPEG/PNG/GIF/WebP) under MAX_INLINE_IMAGE_BYTES: sent as a
      real MCP ImageContent block (the raw bytes, base64-encoded, with
      their mimeType) — this is the protocol's native image type, so
      Claude renders the actual picture directly instead of needing to
      fetch and render an external link.
    - Everything else (PDF, docx, xlsx, zip, oversized images, ...):
      returned as an explicit, directly-fetchable HTTPS download link.

    Every attachment also always gets its download link included, so the
    file can be saved regardless of whether it was also shown inline.
    """
    if not att.content:
        return [
            TextContent(
                type="text",
                text=f"'{att.filename}' has no stored content and can't be "
                     f"displayed. Try calling sync_missing_attachments to "
                     f"download it, then retry.",
            )
        ]

    url = attachment_download_url(att)
    mime_type = (att.mime_type or "").lower()
    size_bytes = att.size or len(att.content)
    size_kb = size_bytes / 1024

    download_line = TextContent(
        type="text",
        text=f"⬇️ [Download {att.filename} ({size_kb:.1f} KB)]({url})",
    )

    if mime_type in INLINE_IMAGE_MIME_TYPES and size_bytes <= MAX_INLINE_IMAGE_BYTES:
        image_block = ImageContent(
            type="image",
            data=base64.b64encode(att.content).decode("utf-8"),
            mimeType=att.mime_type,
        )
        return [image_block, download_line]

    if mime_type.startswith("image/"):
        # SVG or an oversized raster image: no reliable inline render,
        # link only.
        label = "🖼️"
    elif "pdf" in mime_type:
        label = "📄"
    else:
        label = "📎"

    return [
        TextContent(
            type="text",
            text=f"{label} {att.filename} ({size_kb:.1f} KB, "
                 f"{att.mime_type or 'unknown type'})",
        ),
        download_line,
    ]


def query(stmt):
    db = SessionLocal()
    try:
        return [serialize(e) for e in db.scalars(stmt).all()]
    finally:
        db.close()


# ============================================================================
# EXISTING SEARCH TOOLS (unchanged)
# ============================================================================

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
    """Get all emails in a thread by thread ID."""
    return query(
        select(Email).where(Email.thread_id == thread_id).order_by(Email.received_at)
    )


@mcp.tool()
def search_by_sender(sender: str, limit: int | None = None) -> list[dict]:
    """Search emails by sender email address."""
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
    """Search emails by subject line."""
    stmt = (
        select(Email)
        .where(Email.subject.ilike(f"%{subject}%"))
        .order_by(Email.received_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return query(stmt)


# ============================================================================
# NEW ATTACHMENT TOOLS - Display attachments in Claude
# ============================================================================

@mcp.tool()
def list_attachments(email_id: int) -> list[dict]:
    """
    List all attachments for an email with their metadata.
    
    Returns: List of attachment objects with:
    - id: Internal attachment ID (use for other attachment tools)
    - filename: Original filename
    - mime_type: MIME type (e.g., 'image/jpeg', 'application/pdf')
    - size: File size in bytes
    - is_displayable: True if attachment can be shown in Claude (images, PDFs, text)
    - has_content: True if binary content is stored in database
    """
    db = SessionLocal()
    try:
        email = db.scalar(select(Email).where(Email.id == email_id))
        
        if not email:
            return {"error": f"Email with ID {email_id} not found"}
        
        attachments = db.scalars(
            select(EmailAttachment).where(EmailAttachment.email_id == email_id)
        ).all()
        
        return [serialize_attachment(att) for att in attachments]
    finally:
        db.close()


@mcp.tool(structured_output=False)
def get_attachment_content(attachment_id: int, include_link: bool = True):
    """
    Retrieve one attachment so Claude can actually display/download it.

    Images render inline (real MCP image content, not just a link).
    PDFs and any other file type (docx, xlsx, zip, ...) get an explicit
    download link.

    Args:
        attachment_id: Internal attachment ID (from list_attachments)
        include_link: If True (default), includes the image/download link.
                     If False, returns metadata only.
    """
    db = SessionLocal()
    try:
        attachment = db.scalar(
            select(EmailAttachment).where(EmailAttachment.id == attachment_id)
        )

        if not attachment:
            return [
                TextContent(
                    type="text",
                    text=f"Attachment with ID {attachment_id} not found.",
                )
            ]

        size_kb = (attachment.size or 0) / 1024
        meta = TextContent(
            type="text",
            text=(
                f"📎 {attachment.filename}\n"
                f"Type: {attachment.mime_type or 'unknown'}\n"
                f"Size: {size_kb:.1f} KB"
            ),
        )

        if not include_link:
            return [meta]

        return [meta, *attachment_to_content_blocks(attachment)]
    finally:
        db.close()


@mcp.tool()
def extract_attachment_text(attachment_id: int) -> dict:
    """
    Extract plain text from document attachments.
    
    Supported formats:
    - PDF (via PyMuPDF)
    - DOCX, DOC (via python-docx)
    - XLSX, XLS (via openpyxl)
    - PPTX (via python-pptx)
    - Plain text files
    - RTF (via striprtf)
    
    Args:
        attachment_id: Internal attachment ID
    
    Returns:
    {
        "filename": str,
        "mime_type": str,
        "extracted_text": str (or null if extraction failed),
        "character_count": int,
        "extraction_method": str (e.g., "pdf", "docx", "text")
    }
    """
    db = SessionLocal()
    try:
        attachment = db.scalar(
            select(EmailAttachment).where(EmailAttachment.id == attachment_id)
        )
        
        if not attachment:
            return {"error": f"Attachment with ID {attachment_id} not found"}
        
        result = {
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
        }
        
        # Attempt text extraction
        text = extract_text_from_attachment(db, attachment_id)
        
        if text:
            result["extracted_text"] = text
            result["character_count"] = len(text)
            result["status"] = "success"
        else:
            result["extracted_text"] = None
            result["character_count"] = 0
            result["status"] = "failed"
            result["message"] = "Could not extract text from this attachment type"
        
        return result
    finally:
        db.close()


@mcp.tool(structured_output=False)
def get_email_with_attachments(email_id: int, include_content: bool = True):
    """
    Get a complete email formatted for reading, with attachments actually
    rendered/downloadable in Claude (not just described in JSON).

    Args:
        email_id: Internal email ID
        include_content: If True (default), renders each stored attachment:
                        images inline, everything else (PDF, docx, xlsx, ...)
                        as a downloadable file. Set False for text-only output.
    """
    db = SessionLocal()
    try:
        email = db.scalar(select(Email).where(Email.id == email_id))

        if not email:
            return [
                TextContent(type="text", text=f"Email with ID {email_id} not found.")
            ]

        recipients = group_recipients(email.recipients)

        def fmt_people(people: list[dict]) -> str:
            parts = []
            for p in people:
                name, addr = p.get("name"), p.get("email")
                parts.append(f"{name} <{addr}>" if name else str(addr))
            return ", ".join(parts) if parts else "(none)"

        attachments = db.scalars(
            select(EmailAttachment).where(EmailAttachment.email_id == email_id)
        ).all()

        lines = [
            "📧 EMAIL DETAILS",
            f"From: {email.sender_name} <{email.sender_email}>"
            if email.sender_name
            else f"From: {email.sender_email}",
            f"To: {fmt_people(recipients['to'])}",
        ]
        if recipients["cc"]:
            lines.append(f"Cc: {fmt_people(recipients['cc'])}")
        lines += [
            f"Subject: {email.subject or '(no subject)'}",
            f"Date: {email.received_at.strftime('%B %d, %Y')}",
            "",
            "📝 EMAIL BODY:",
            email.body_text or "(no body text)",
        ]

        if attachments:
            lines += ["", "📎 ATTACHMENTS"]
            for att in attachments:
                size_kb = (att.size or 0) / 1024
                lines.append(
                    f"- {att.filename} ({size_kb:.1f} KB, {att.mime_type or 'unknown type'})"
                )

        blocks: list = [TextContent(type="text", text="\n".join(lines))]

        if include_content:
            for att in attachments:
                blocks.extend(attachment_to_content_blocks(att))

        return blocks
    finally:
        db.close()


@mcp.tool()
def search_emails_with_attachments(
    query_text: str,
    attachment_type: str = "any",
    limit: int | None = 10
) -> list[dict]:
    """
    Search for emails that contain attachments of a specific type.
    
    Args:
        query_text: Search in subject, body, or sender
        attachment_type: Filter by attachment type:
                        - "any": Has any attachment
                        - "image": Contains images (JPEG, PNG, GIF, WebP)
                        - "pdf": Contains PDFs
                        - "document": Contains Word docs, PDFs, etc.
                        - "spreadsheet": Contains Excel, CSV
                        - "archive": Contains ZIP, RAR, TAR
        limit: Maximum results
    
    Returns: List of emails that have matching attachments
    """
    db = SessionLocal()
    try:
        # First find emails matching search criteria
        q = f"%{query_text}%"
        stmt = (
            select(Email)
            .where(
                Email.has_attachments == True,
                or_(
                    Email.sender_email.ilike(q),
                    Email.subject.ilike(q),
                    Email.body_text.ilike(q),
                )
            )
            .order_by(Email.received_at.desc())
        )
        
        emails = db.scalars(stmt).all()
        
        # Filter by attachment type if needed
        if attachment_type != "any":
            filtered_emails = []
            
            for email in emails:
                for att_meta in email.attachments or []:
                    att_type = att_meta.get("type", "")
                    if att_type == attachment_type:
                        filtered_emails.append(email)
                        break
            
            emails = filtered_emails
        
        if limit:
            emails = emails[:limit]
        
        return [serialize(e) for e in emails]
    finally:
        db.close()


@mcp.tool()
def sync_missing_attachments(limit: int | None = None) -> dict:
    """
    Downloads the actual attachment bytes for emails already stored in
    the database whose metadata says they have attachments but whose
    content was never downloaded (stored_attachments_count is less than
    the number of attachments listed) — e.g. emails synced before
    attachment downloading was fully wired up.

    Call this when list_emails / search results show attachments in
    metadata but get_attachment_content / get_email_with_attachments
    can't display or download them (has_content: false, or
    stored_attachments_count 0 while attachments is non-empty).

    Args:
        limit: Max number of such emails to backfill in this call. Omit
               to attempt all of them (may take a while on a large
               backlog — call again with a limit if it times out).

    Returns:
        {
            "emails_checked": int,
            "emails_backfilled": list[int],
            "attachments_stored": int,
            "attachments_failed": int,
        }
    """
    db = SessionLocal()
    try:
        return backfill_missing_attachments(db, limit=limit)
    finally:
        db.close()