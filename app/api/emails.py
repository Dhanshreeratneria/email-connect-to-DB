from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.email import Email
from app.schemas.email import EmailOut

router = APIRouter()


@router.get("/emails", response_model=list[EmailOut])
async def get_emails(
    q: str | None = None,
    sender: str | None = None,
    subject: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db)
) -> list[EmailOut]:
    """
    Retrieve emails with optional filtering.
    
    Query Parameters:
    - q: Search in subject and body
    - sender: Filter by sender email
    - subject: Filter by subject line
    - limit: Maximum results (1-200, default 50)
    """
    try:
        stmt = select(Email).order_by(Email.received_at.desc()).limit(limit)
        
        if q:
            stmt = stmt.where(
                or_(
                    Email.subject.ilike(f"%{q}%"),
                    Email.body_text.ilike(f"%{q}%"),
                    Email.sender_email.ilike(f"%{q}%")
                )
            )
        
        if sender:
            stmt = stmt.where(Email.sender_email.ilike(f"%{sender}%"))
        
        if subject:
            stmt = stmt.where(Email.subject.ilike(f"%{subject}%"))
        
        emails = db.scalars(stmt).all()
        return emails
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve emails: {str(e)}"
        )


@router.get("/emails/{message_id}", response_model=EmailOut)
async def get_email(
    message_id: str,
    db: Session = Depends(get_db)
) -> EmailOut:
    """
    Retrieve a specific email by message ID.
    
    Path Parameters:
    - message_id: Gmail message ID
    """
    try:
        email = db.scalar(
            select(Email).where(Email.message_id == message_id)
        )
        
        if not email:
            raise HTTPException(
                status_code=404,
                detail=f"Email with message_id '{message_id}' not found"
            )
        
        return email
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve email: {str(e)}"
        )