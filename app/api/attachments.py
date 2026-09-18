"""
New FastAPI endpoints for attachment download and retrieval.

These endpoints allow downloading attachments as:
- Binary (raw file download)
- Base64 (for Claude/MCP display)
- Text (extracted from PDFs, docs, etc.)
- Metadata (info only, no content)
"""

import base64
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.email import Email, EmailAttachment, EmailDelivery
from app.services.attachment_service import (
    get_attachment_content_base64,
    extract_text_from_attachment,
    get_image_dimensions,
    is_displayable_in_claude,
)
from app.services.sync_service import backfill_missing_attachments
from app.schemas.email import AttachmentOut

logger = logging.getLogger(__name__)
router = APIRouter()


# ============================================================================
# EXISTING ENDPOINTS (from emails_api.py - reference only)
# ============================================================================
# These endpoints already exist, included here for reference:
#
# GET /emails/{message_id}/attachments
#   - List attachment metadata for an email
#
# GET /emails/{message_id}/attachments/{attachment_id}/download
#   - Download attachment as binary file (raw bytes)


# ============================================================================
# NEW ATTACHMENT-SPECIFIC ENDPOINTS
# ============================================================================

@router.post("/attachments/backfill")
async def backfill_attachments(
    limit: Optional[int] = Query(
        None, description="Max emails to backfill in this call. Omit for all."
    ),
    db: Session = Depends(get_db),
) -> dict:
    """
    Downloads attachment bytes for already-synced emails whose metadata
    says they have attachments but whose content was never stored (e.g.
    emails synced before attachment downloading was fully wired up —
    those were permanently stuck at stored_attachments_count: 0).

    Safe to call repeatedly: each attachment is only downloaded once
    (existing rows are skipped), so this is a no-op once everything is
    backfilled.
    """
    try:
        return backfill_missing_attachments(db, limit=limit)
    except Exception as e:
        logger.error(f"Attachment backfill failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Backfill failed: {str(e)}")


@router.get("/attachments/{attachment_id}/metadata")
async def get_attachment_metadata(
    attachment_id: int,
    db: Session = Depends(get_db)
) -> dict:
    """
    Get metadata for an attachment without downloading content.
    
    Returns:
    {
        "id": int,
        "filename": str,
        "mime_type": str,
        "size": int (bytes),
        "gmail_attachment_id": str,
        "created_at": str (ISO format),
        "is_displayable": bool,
        "has_content": bool,
        "content_type_category": str (image, pdf, document, etc.)
    }
    """
    try:
        attachment = db.scalar(
            select(EmailAttachment).where(EmailAttachment.id == attachment_id)
        )
        
        if not attachment:
            raise HTTPException(status_code=404, detail="Attachment not found")
        
        mime_type = attachment.mime_type or ""
        
        # Categorize file type
        if mime_type.startswith("image/"):
            category = "image"
        elif "pdf" in mime_type:
            category = "pdf"
        elif "word" in mime_type or "document" in mime_type:
            category = "document"
        elif "sheet" in mime_type or "excel" in mime_type or "csv" in mime_type:
            category = "spreadsheet"
        elif "presentation" in mime_type or "powerpoint" in mime_type:
            category = "presentation"
        elif "zip" in mime_type or "rar" in mime_type or "tar" in mime_type or "gzip" in mime_type:
            category = "archive"
        elif mime_type.startswith("text/"):
            category = "text"
        else:
            category = "other"
        
        return {
            "id": attachment.id,
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "size": attachment.size,
            "gmail_attachment_id": attachment.gmail_attachment_id,
            "created_at": attachment.created_at.isoformat() if attachment.created_at else None,
            "is_displayable": is_displayable_in_claude(attachment.mime_type),
            "has_content": attachment.content is not None,
            "content_type_category": category,
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get attachment metadata: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get attachment: {str(e)}")


@router.get("/attachments/{attachment_id}/base64")
async def get_attachment_as_base64(
    attachment_id: int,
    db: Session = Depends(get_db)
) -> dict:
    """
    Download attachment as base64-encoded string for display in Claude or other tools.
    
    Returns:
    {
        "filename": str,
        "mime_type": str,
        "size": int (original file size),
        "encoded_size": int (base64 encoded size),
        "content_base64": str,
        "can_display_inline": bool
    }
    
    Use case: Send attachment to Claude for analysis or display (especially images/PDFs)
    """
    try:
        result = get_attachment_content_base64(db, attachment_id)
        
        if not result:
            raise HTTPException(status_code=404, detail="Attachment not found or has no content")
        
        base64_content, mime_type, filename = result
        
        return {
            "filename": filename,
            "mime_type": mime_type,
            "size": len(base64.b64decode(base64_content)),
            "encoded_size": len(base64_content),
            "content_base64": base64_content,
            "can_display_inline": is_displayable_in_claude(mime_type),
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get attachment as base64: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to encode attachment: {str(e)}")


@router.get("/attachments/{attachment_id}/text")
async def extract_attachment_text(
    attachment_id: int,
    max_length: Optional[int] = Query(None, description="Truncate extracted text to this length"),
    db: Session = Depends(get_db)
) -> dict:
    """
    Extract plain text from document attachments (PDF, Word, Excel, PowerPoint, etc.).
    
    Supported formats:
    - PDF (via PyMuPDF)
    - DOCX, DOC (via python-docx)
    - XLSX, XLS (via openpyxl)
    - PPTX (via python-pptx)
    - Plain text files
    - RTF (via striprtf)
    - CSV (returns structured rows)
    - JSON (returns pretty-printed)
    
    Returns:
    {
        "filename": str,
        "mime_type": str,
        "extraction_status": "success" | "failed" | "unsupported",
        "extracted_text": str (or null),
        "character_count": int,
        "truncated": bool,
        "error_message": str (if failed)
    }
    """
    try:
        attachment = db.scalar(
            select(EmailAttachment).where(EmailAttachment.id == attachment_id)
        )
        
        if not attachment:
            raise HTTPException(status_code=404, detail="Attachment not found")
        
        result = {
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
        }
        
        # Check if format is supported
        if not attachment.content:
            result["extraction_status"] = "failed"
            result["extracted_text"] = None
            result["error_message"] = "Attachment has no stored content"
            return result
        
        # Attempt extraction
        text = extract_text_from_attachment(db, attachment_id)
        
        if text:
            # Optionally truncate
            if max_length and len(text) > max_length:
                text = text[:max_length]
                result["truncated"] = True
            else:
                result["truncated"] = False
            
            result["extraction_status"] = "success"
            result["extracted_text"] = text
            result["character_count"] = len(text)
        else:
            result["extraction_status"] = "failed"
            result["extracted_text"] = None
            result["character_count"] = 0
            result["error_message"] = "Could not extract text from this attachment format"
        
        return result
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to extract attachment text: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")


@router.get("/attachments/{attachment_id}/image-info")
async def get_image_info(
    attachment_id: int,
    db: Session = Depends(get_db)
) -> dict:
    """
    Get image metadata (dimensions, format info) for display purposes.
    
    Works with: JPEG, PNG, GIF, WebP, SVG
    
    Returns:
    {
        "filename": str,
        "mime_type": str,
        "size": int (bytes),
        "is_image": bool,
        "width": int (pixels),
        "height": int (pixels),
        "aspect_ratio": float,
        "display_info": str
    }
    """
    try:
        attachment = db.scalar(
            select(EmailAttachment).where(EmailAttachment.id == attachment_id)
        )
        
        if not attachment:
            raise HTTPException(status_code=404, detail="Attachment not found")
        
        result = {
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "size": attachment.size,
            "is_image": attachment.mime_type and attachment.mime_type.startswith("image/"),
        }
        
        if result["is_image"]:
            dimensions = get_image_dimensions(db, attachment_id)
            
            if dimensions:
                width, height = dimensions
                result["width"] = width
                result["height"] = height
                result["aspect_ratio"] = width / height if height > 0 else 0
                result["display_info"] = f"{width}x{height}px"
            else:
                result["error"] = "Could not determine image dimensions"
        
        return result
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get image info: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get image info: {str(e)}")


@router.get("/emails/{email_id}/attachments/summary")
async def get_email_attachments_summary(
    email_id: int,
    db: Session = Depends(get_db)
) -> dict:
    """
    Get summary of all attachments for an email with their status.
    
    Returns:
    {
        "email_id": int,
        "total_attachments": int,
        "total_size": int (bytes),
        "attachments": [
            {
                "id": int,
                "filename": str,
                "mime_type": str,
                "size": int,
                "category": str,
                "is_displayable": bool,
                "has_content": bool
            }
        ]
    }
    """
    try:
        email = db.scalar(select(Email).where(Email.id == email_id))
        
        if not email:
            raise HTTPException(status_code=404, detail="Email not found")
        
        attachments = db.scalars(
            select(EmailAttachment).where(EmailAttachment.email_id == email_id)
        ).all()
        
        att_list = []
        total_size = 0
        
        for att in attachments:
            # Categorize
            mime_type = att.mime_type or ""
            if mime_type.startswith("image/"):
                category = "image"
            elif "pdf" in mime_type:
                category = "pdf"
            elif "word" in mime_type or "document" in mime_type:
                category = "document"
            elif "sheet" in mime_type or "excel" in mime_type:
                category = "spreadsheet"
            elif "presentation" in mime_type:
                category = "presentation"
            elif "zip" in mime_type or "archive" in mime_type:
                category = "archive"
            else:
                category = "other"
            
            att_dict = {
                "id": att.id,
                "filename": att.filename,
                "mime_type": att.mime_type,
                "size": att.size,
                "category": category,
                "is_displayable": is_displayable_in_claude(att.mime_type),
                "has_content": att.content is not None,
            }
            
            att_list.append(att_dict)
            if att.size:
                total_size += att.size
        
        return {
            "email_id": email_id,
            "total_attachments": len(attachments),
            "total_size": total_size,
            "total_size_mb": total_size / (1024 * 1024),
            "attachments": att_list,
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get attachments summary: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get summary: {str(e)}")