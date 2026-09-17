"""
Service module for downloading and processing email attachments from Gmail.

Handles:
- Downloading attachment bytes from Gmail API
- Storing binary content in PostgreSQL
- Converting attachments to displayable formats (base64, text extraction, etc.)
- Error handling and logging
"""

import base64
import logging
import time
from io import BytesIO
from typing import Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.models.email import Email, EmailAttachment
from app.services.gmail_service import get_attachment
from app.services.email_parser import decode_base64_urlsafe_bytes

logger = logging.getLogger(__name__)

# Delay between API calls to avoid rate limiting
API_CALL_DELAY = 0.1


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
    """
    Downloads attachment bytes from Gmail API and stores them in PostgreSQL.
    
    Args:
        db: SQLAlchemy session
        email_id: Internal email ID
        gmail_message_id: Gmail message ID
        gmail_attachment_id: Gmail attachment ID
        filename: Original filename
        mime_type: MIME type (e.g., 'application/pdf')
        size: File size in bytes
        gmail_service: Gmail API service instance
        
    Returns:
        EmailAttachment object if successful, None if failed
    """
    
    # Check if already stored
    existing = db.scalar(
        select(EmailAttachment).where(
            EmailAttachment.email_id == email_id,
            EmailAttachment.gmail_attachment_id == gmail_attachment_id,
        )
    )
    
    if existing:
        logger.debug(f"Attachment {gmail_attachment_id} already stored")
        return existing
    
    try:
        # Rate limiting
        time.sleep(API_CALL_DELAY)
        
        # Download from Gmail
        raw = get_attachment(gmail_service, gmail_message_id, gmail_attachment_id)
        content = decode_base64_urlsafe_bytes(raw.get("data", ""))
        
        # Store in database
        attachment = EmailAttachment(
            email_id=email_id,
            gmail_attachment_id=gmail_attachment_id,
            filename=filename or "attachment",
            mime_type=mime_type,
            size=size or len(content),
            content=content,
        )
        
        db.add(attachment)
        db.commit()
        
        logger.info(
            f"Stored attachment {filename} ({len(content)} bytes) "
            f"for email {email_id}"
        )
        
        return attachment
        
    except Exception as e:
        logger.error(
            f"Failed to download attachment {gmail_attachment_id} "
            f"for message {gmail_message_id}: {str(e)}"
        )
        db.rollback()
        return None


def get_attachment_content_base64(
    db: Session,
    attachment_id: int,
) -> Optional[Tuple[str, str, str]]:
    """
    Retrieves attachment content as base64 for display in Claude.
    
    Args:
        db: SQLAlchemy session
        attachment_id: Internal attachment ID
        
    Returns:
        Tuple of (base64_content, mime_type, filename) or None if not found
    """
    
    attachment = db.scalar(
        select(EmailAttachment).where(EmailAttachment.id == attachment_id)
    )
    
    if not attachment or not attachment.content:
        logger.warning(f"Attachment {attachment_id} not found or has no content")
        return None
    
    base64_content = base64.b64encode(attachment.content).decode("utf-8")
    
    return (
        base64_content,
        attachment.mime_type or "application/octet-stream",
        attachment.filename,
    )


def get_email_attachments_with_content(
    db: Session,
    email_id: int,
) -> list[dict]:
    """
    Retrieves all attachments for an email with their content as base64.
    
    Useful for displaying in Claude or other tools.
    
    Args:
        db: SQLAlchemy session
        email_id: Internal email ID
        
    Returns:
        List of attachment dicts with: id, filename, mime_type, size, 
        content_base64, is_displayable
    """
    
    attachments = db.scalars(
        select(EmailAttachment).where(EmailAttachment.email_id == email_id)
    ).all()
    
    result = []
    
    for att in attachments:
        att_dict = {
            "id": att.id,
            "filename": att.filename,
            "mime_type": att.mime_type,
            "size": att.size,
            "gmail_attachment_id": att.gmail_attachment_id,
        }
        
        # Add base64 content if available
        if att.content:
            att_dict["content_base64"] = base64.b64encode(att.content).decode("utf-8")
            att_dict["is_displayable"] = is_displayable_in_claude(att.mime_type)
        else:
            att_dict["content_base64"] = None
            att_dict["is_displayable"] = False
        
        result.append(att_dict)
    
    return result


def is_displayable_in_claude(mime_type: Optional[str]) -> bool:
    """
    Determines if attachment type can be displayed directly in Claude.
    
    Displayable types:
    - Images: image/jpeg, image/png, image/gif, image/webp
    - PDFs: application/pdf
    - Text: text/plain, text/csv, text/xml, application/json
    
    Args:
        mime_type: MIME type string
        
    Returns:
        True if displayable, False otherwise
    """
    
    if not mime_type:
        return False
    
    displayable_types = {
        "image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml",
        "application/pdf",
        "text/plain", "text/csv", "text/xml", "application/json",
    }
    
    return mime_type.lower() in displayable_types


def extract_text_from_attachment(
    db: Session,
    attachment_id: int,
) -> Optional[str]:
    """
    Attempts to extract plain text from various document types.
    
    Supported formats:
    - PDF (via PyMuPDF/fitz)
    - DOCX (via python-docx)
    - XLSX (via openpyxl)
    - PPTX (via python-pptx)
    - Plain text files
    - RTF (via striprtf)
    
    Args:
        db: SQLAlchemy session
        attachment_id: Internal attachment ID
        
    Returns:
        Extracted text string, or None if extraction failed
    """
    
    attachment = db.scalar(
        select(EmailAttachment).where(EmailAttachment.id == attachment_id)
    )
    
    if not attachment or not attachment.content:
        return None
    
    mime_type = (attachment.mime_type or "").lower()
    
    try:
        # PDF extraction
        if "pdf" in mime_type:
            import fitz  # PyMuPDF
            
            doc = fitz.open(stream=attachment.content, filetype="pdf")
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
            return text.strip() if text.strip() else None
        
        # DOCX extraction
        elif "wordprocessingml" in mime_type or mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            from docx import Document
            
            doc = Document(BytesIO(attachment.content))
            text = "\n".join(para.text for para in doc.paragraphs)
            return text.strip() if text.strip() else None
        
        # XLSX extraction
        elif "spreadsheetml" in mime_type or mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            from openpyxl import load_workbook
            
            wb = load_workbook(BytesIO(attachment.content))
            text_lines = []
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                text_lines.append(f"Sheet: {sheet}")
                for row in ws.iter_rows(values_only=True):
                    text_lines.append("\t".join(str(v) for v in row if v is not None))
            return "\n".join(text_lines).strip()
        
        # PPTX extraction
        elif "presentationml" in mime_type:
            from pptx import Presentation
            
            prs = Presentation(BytesIO(attachment.content))
            text_lines = []
            for slide_num, slide in enumerate(prs.slides, 1):
                text_lines.append(f"Slide {slide_num}:")
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        text_lines.append(shape.text)
            return "\n".join(text_lines).strip()
        
        # Plain text
        elif "text/plain" in mime_type:
            return attachment.content.decode("utf-8", errors="replace").strip()
        
        # RTF extraction
        elif "rtf" in mime_type:
            from striprtf.striprtf import rtf_to_text
            
            text = rtf_to_text(attachment.content.decode("utf-8", errors="replace"))
            return text.strip() if text.strip() else None
        
    except Exception as e:
        logger.error(f"Failed to extract text from attachment {attachment_id}: {str(e)}")
        return None
    
    return None


def get_image_dimensions(
    db: Session,
    attachment_id: int,
) -> Optional[Tuple[int, int]]:
    """
    Gets image dimensions (width, height) for display purposes.
    
    Works with: JPEG, PNG, GIF, WebP, SVG
    
    Args:
        db: SQLAlchemy session
        attachment_id: Internal attachment ID
        
    Returns:
        Tuple of (width, height) or None if not an image or failed
    """
    
    attachment = db.scalar(
        select(EmailAttachment).where(EmailAttachment.id == attachment_id)
    )
    
    if not attachment or not attachment.content:
        return None
    
    mime_type = (attachment.mime_type or "").lower()
    
    if not mime_type.startswith("image/"):
        return None
    
    try:
        from PIL import Image
        
        img = Image.open(BytesIO(attachment.content))
        return img.size  # (width, height)
        
    except Exception as e:
        logger.error(f"Failed to get image dimensions for attachment {attachment_id}: {str(e)}")
        return None