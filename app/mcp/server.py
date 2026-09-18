"""
Updated MCP Server for Gmail Email Search with full attachment support.

New Features:
- get_attachment_content: Returns attachment as base64 for display in Claude
- extract_attachment_text: Extracts text from PDFs, docs, spreadsheets, etc.
- get_email_with_attachments: Returns email with all attachments as base64
- list_attachments: List attachments for an email with displayable status

All attachments stored in PostgreSQL are now accessible to Claude.
"""

import base64
from datetime import datetime
from urllib.parse import quote

from sqlalchemy import or_, select
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ImageContent, EmbeddedResource, BlobResourceContents

from app.database import SessionLocal
from app.models.email import Email, EmailDelivery, EmailAttachment
from app.services.attachment_service import (
    get_attachment_content_base64,
    get_email_attachments_with_content,
    extract_text_from_attachment,
    is_displayable_in_claude,
)

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


def attachment_to_content_blocks(att: EmailAttachment) -> list:
    """
    Converts one stored attachment into real MCP content blocks instead of a
    JSON blob with a base64 string field.

    - image/*        -> ImageContent, so Claude renders it inline.
    - everything else (PDF, docx, xlsx, zip, ...) -> EmbeddedResource with a
      blob, so Claude shows/download the actual file instead of raw text.

    This is what actually makes attachments show up and be downloadable in
    Claude; returning a dict with a "content_base64" key only produces one
    big TextContent block that Claude can't render as a file.
    """
    if not att.content:
        return [
            TextContent(
                type="text",
                text=f"'{att.filename}' has no stored content and can't be displayed.",
            )
        ]

    mime_type = (att.mime_type or "application/octet-stream").lower()
    b64 = base64.b64encode(att.content).decode("utf-8")

    if mime_type.startswith("image/"):
        return [ImageContent(type="image", data=b64, mimeType=mime_type)]

    uri = f"attachment://{att.id}/{quote(att.filename or 'attachment')}"
    return [
        EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri=uri,
                mimeType=mime_type,
                blob=b64,
            ),
        )
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
def get_attachment_content(attachment_id: int, return_base64: bool = True):
    """
    Retrieve one attachment so Claude can actually display/download it.

    Images (JPEG, PNG, GIF, WebP) render inline. PDFs and any other file
    type (docx, xlsx, zip, ...) come back as an embedded resource with a
    download affordance in Claude.

    Args:
        attachment_id: Internal attachment ID (from list_attachments)
        return_base64: If True (default), includes the actual file so it can
                      be shown/downloaded. If False, returns metadata only.
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

        if not return_base64:
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