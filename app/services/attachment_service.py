"""Attachment download, storage, classification, extraction and rendering."""

from __future__ import annotations

import base64
import logging
import mimetypes
import time
from io import BytesIO
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.email import EmailAttachment
from app.services.email_parser import decode_base64_urlsafe_bytes
from app.services.gmail_service import get_attachment

logger = logging.getLogger(__name__)
API_CALL_DELAY = 0.1


def attachment_category(mime_type: Optional[str], filename: Optional[str] = None) -> str:
    mime = (mime_type or "").lower()
    name = (filename or "").lower()

    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if "word" in mime or "wordprocessingml" in mime or name.endswith((".doc", ".docx")):
        return "document"
    if "excel" in mime or "spreadsheet" in mime or mime == "text/csv" or name.endswith((".xls", ".xlsx", ".csv")):
        return "spreadsheet"
    if "presentation" in mime or "powerpoint" in mime or name.endswith((".ppt", ".pptx")):
        return "presentation"
    if mime.startswith("text/") or "json" in mime or name.endswith((".txt", ".json", ".xml", ".rtf")):
        return "text"
    if any(x in mime for x in ("zip", "rar", "7z", "tar", "gzip")) or name.endswith((".zip", ".rar", ".7z", ".tar", ".gz")):
        return "archive"
    return "other"


def download_and_store_attachment(
    db: Session,
    email_id: int,
    gmail_message_id: str,
    gmail_attachment_id: str,
    filename: str,
    mime_type: Optional[str],
    size: Optional[int],
    gmail_service,
) -> Optional[EmailAttachment]:
    """Download Gmail attachment bytes and store them in PostgreSQL."""
    existing = db.scalar(
        select(EmailAttachment).where(
            EmailAttachment.email_id == email_id,
            EmailAttachment.gmail_attachment_id == gmail_attachment_id,
        )
    )
    if existing:
        return existing

    try:
        time.sleep(API_CALL_DELAY)
        raw = get_attachment(gmail_service, gmail_message_id, gmail_attachment_id)
        content = decode_base64_urlsafe_bytes(raw.get("data", ""))

        if not content:
            logger.warning(
                "Gmail returned no content for attachment %s",
                gmail_attachment_id,
            )
            return None

        attachment = EmailAttachment(
            email_id=email_id,
            gmail_attachment_id=gmail_attachment_id,
            filename=filename or "attachment",
            mime_type=mime_type or mimetypes.guess_type(filename or "")[0],
            size=size or len(content),
            content=content,
        )
        db.add(attachment)
        db.flush()
        logger.info(
            "Stored attachment filename=%s email_id=%s bytes=%s",
            filename,
            email_id,
            len(content),
        )
        return attachment
    except Exception:
        logger.exception(
            "Failed to download attachment %s for Gmail message %s",
            gmail_attachment_id,
            gmail_message_id,
        )
        return None


def get_attachment_content_base64(
    db: Session,
    attachment_id: int,
) -> Optional[Tuple[str, str, str]]:
    attachment = db.get(EmailAttachment, attachment_id)
    if not attachment or not attachment.content:
        return None
    return (
        base64.b64encode(attachment.content).decode("ascii"),
        attachment.mime_type or "application/octet-stream",
        attachment.filename,
    )


def is_displayable_in_claude(mime_type: Optional[str]) -> bool:
    """Whether the type can be represented directly by this MCP server."""
    return bool(mime_type and (
        mime_type.lower().startswith("image/")
        or mime_type.lower() == "application/pdf"
    ))


def extract_text_from_attachment(
    attachment: EmailAttachment,
) -> Optional[str]:
    """Extract readable text from common office/document formats."""
    if not attachment.content:
        return None

    mime = (attachment.mime_type or "").lower()
    name = (attachment.filename or "").lower()
    data = attachment.content

    try:
        if mime == "application/pdf" or name.endswith(".pdf"):
            import fitz
            doc = fitz.open(stream=data, filetype="pdf")
            try:
                text = "\n".join(page.get_text() for page in doc)
            finally:
                doc.close()
            return text.strip() or None

        if "wordprocessingml" in mime or name.endswith(".docx"):
            from docx import Document
            doc = Document(BytesIO(data))
            parts = [p.text for p in doc.paragraphs if p.text]
            for table in doc.tables:
                for row in table.rows:
                    parts.append("\t".join(cell.text for cell in row.cells))
            return "\n".join(parts).strip() or None

        if "spreadsheetml" in mime or mime == "application/vnd.ms-excel" or mime == "text/csv" or name.endswith((".xlsx", ".csv")):
            if mime == "text/csv" or name.endswith(".csv"):
                return data.decode("utf-8-sig", errors="replace").strip() or None
            from openpyxl import load_workbook
            wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
            try:
                lines = []
                for sheet in wb.sheetnames:
                    ws = wb[sheet]
                    lines.append(f"Sheet: {sheet}")
                    for row in ws.iter_rows(values_only=True):
                        values = [str(v) for v in row if v is not None]
                        if values:
                            lines.append("\t".join(values))
                return "\n".join(lines).strip() or None
            finally:
                wb.close()

        if "presentationml" in mime or name.endswith(".pptx"):
            from pptx import Presentation
            prs = Presentation(BytesIO(data))
            lines = []
            for i, slide in enumerate(prs.slides, 1):
                lines.append(f"Slide {i}:")
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text:
                        lines.append(shape.text)
            return "\n".join(lines).strip() or None

        if mime.startswith("text/") or mime == "application/json" or name.endswith((".txt", ".json", ".xml", ".csv")):
            return data.decode("utf-8-sig", errors="replace").strip() or None

        if "rtf" in mime or name.endswith(".rtf"):
            from striprtf.striprtf import rtf_to_text
            text = rtf_to_text(data.decode("utf-8", errors="replace"))
            return text.strip() or None

    except Exception:
        logger.exception(
            "Text extraction failed for attachment id=%s filename=%s",
            attachment.id,
            attachment.filename,
        )

    return None


def render_pdf_pages(
    content: bytes,
    max_pages: int = 5,
    scale: float = 1.5,
) -> list[tuple[int, bytes]]:
    """
    Render PDF pages as PNGs for MCP ImageContent.
    Only a bounded number of pages are rendered to prevent oversized responses.
    """
    import fitz

    pages: list[tuple[int, bytes]] = []
    doc = fitz.open(stream=content, filetype="pdf")
    try:
        count = min(len(doc), max(1, max_pages))
        matrix = fitz.Matrix(scale, scale)
        for index in range(count):
            pix = doc[index].get_pixmap(
                matrix=matrix,
                alpha=False,
            )
            pages.append((index + 1, pix.tobytes("png")))
    finally:
        doc.close()
    return pages


def get_image_dimensions(
    db: Session,
    attachment_id: int,
) -> Optional[Tuple[int, int]]:
    attachment = db.get(EmailAttachment, attachment_id)
    if not attachment or not attachment.content:
        return None
    if not (attachment.mime_type or "").lower().startswith("image/"):
        return None

    try:
        from PIL import Image
        with Image.open(BytesIO(attachment.content)) as image:
            return image.size
    except Exception:
        logger.exception(
            "Failed to read image dimensions for attachment %s",
            attachment_id,
        )
        return None
