"""HTTP endpoints for stored Gmail attachments."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.email import Email, EmailAttachment
from app.services.attachment_service import (
    attachment_category,
    extract_text_from_attachment,
    get_image_dimensions,
)
from app.services.sync_service import backfill_missing_attachments

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["attachments"])


def _get_attachment(db: Session, attachment_id: int) -> EmailAttachment:
    attachment = db.get(EmailAttachment, attachment_id)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if not attachment.content:
        raise HTTPException(
            status_code=404,
            detail="Attachment exists but binary content is not stored",
        )
    return attachment


@router.get("/attachments/{attachment_id}/download")
def download_attachment(
    attachment_id: int,
    db: Session = Depends(get_db),
):
    """Download the original attachment bytes."""
    attachment = _get_attachment(db, attachment_id)
    return Response(
        content=attachment.content,
        media_type=attachment.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{attachment.filename}"',
            "Content-Length": str(len(attachment.content)),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/attachments/{attachment_id}/view")
def view_attachment(
    attachment_id: int,
    db: Session = Depends(get_db),
):
    """Serve an attachment inline when the browser supports the MIME type."""
    attachment = _get_attachment(db, attachment_id)
    return Response(
        content=attachment.content,
        media_type=attachment.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{attachment.filename}"',
            "Content-Length": str(len(attachment.content)),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/attachments/backfill")
def backfill_attachments(
    limit: Optional[int] = Query(None, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """Download missing attachment bytes for already-synced emails."""
    try:
        return backfill_missing_attachments(db, limit=limit)
    except Exception:
        logger.exception("Attachment backfill failed")
        raise HTTPException(status_code=500, detail="Attachment backfill failed")


@router.get("/attachments/{attachment_id}/metadata")
def get_attachment_metadata(
    attachment_id: int,
    db: Session = Depends(get_db),
):
    attachment = db.get(EmailAttachment, attachment_id)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    return {
        "id": attachment.id,
        "email_id": attachment.email_id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size": attachment.size,
        "gmail_attachment_id": attachment.gmail_attachment_id,
        "category": attachment_category(
            attachment.mime_type,
            attachment.filename,
        ),
        "has_content": attachment.content is not None,
        "download_url": f"/api/attachments/{attachment.id}/download",
        "view_url": f"/api/attachments/{attachment.id}/view",
        "created_at": (
            attachment.created_at.isoformat()
            if attachment.created_at
            else None
        ),
    }


@router.get("/attachments/{attachment_id}/base64")
def get_attachment_as_base64(
    attachment_id: int,
    db: Session = Depends(get_db),
):
    attachment = _get_attachment(db, attachment_id)
    encoded = __import__("base64").b64encode(attachment.content).decode("ascii")
    return {
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size": len(attachment.content),
        "encoded_size": len(encoded),
        "content_base64": encoded,
        "download_url": f"/api/attachments/{attachment.id}/download",
        "view_url": f"/api/attachments/{attachment.id}/view",
    }


@router.get("/attachments/{attachment_id}/text")
def extract_attachment_text_api(
    attachment_id: int,
    max_length: Optional[int] = Query(None, ge=1, le=1_000_000),
    db: Session = Depends(get_db),
):
    attachment = db.get(EmailAttachment, attachment_id)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    if not attachment.content:
        return {
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "extraction_status": "failed",
            "extracted_text": None,
            "character_count": 0,
            "error_message": "Attachment has no stored content",
        }

    text = extract_text_from_attachment(attachment)
    if text is None:
        return {
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "extraction_status": "unsupported_or_empty",
            "extracted_text": None,
            "character_count": 0,
        }

    truncated = bool(max_length and len(text) > max_length)
    if truncated:
        text = text[:max_length]

    return {
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "extraction_status": "success",
        "extracted_text": text,
        "character_count": len(text),
        "truncated": truncated,
        "download_url": f"/api/attachments/{attachment.id}/download",
    }


@router.get("/attachments/{attachment_id}/image-info")
def get_image_info(
    attachment_id: int,
    db: Session = Depends(get_db),
):
    attachment = db.get(EmailAttachment, attachment_id)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    is_image = (attachment.mime_type or "").lower().startswith("image/")
    result = {
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size": attachment.size,
        "is_image": is_image,
    }

    if is_image:
        dimensions = get_image_dimensions(db, attachment_id)
        if dimensions:
            width, height = dimensions
            result.update({
                "width": width,
                "height": height,
                "aspect_ratio": width / height if height else 0,
            })
    return result


@router.get("/emails/{email_id}/attachments/summary")
def get_email_attachments_summary(
    email_id: int,
    db: Session = Depends(get_db),
):
    email = db.get(Email, email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    attachments = db.execute(
        select(EmailAttachment)
        .where(EmailAttachment.email_id == email_id)
        .order_by(EmailAttachment.id.asc())
    ).scalars().all()

    total_size = sum(
        (a.size or len(a.content or b""))
        for a in attachments
    )

    return {
        "email_id": email_id,
        "total_attachments": len(attachments),
        "total_size": total_size,
        "total_size_mb": round(total_size / (1024 * 1024), 3),
        "attachments": [
            {
                "id": a.id,
                "filename": a.filename,
                "mime_type": a.mime_type,
                "size": a.size,
                "category": attachment_category(a.mime_type, a.filename),
                "has_content": a.content is not None,
                "download_url": f"/api/attachments/{a.id}/download",
                "view_url": f"/api/attachments/{a.id}/view",
            }
            for a in attachments
        ],
    }
