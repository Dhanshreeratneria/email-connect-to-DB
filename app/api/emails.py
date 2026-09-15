from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.email import Email, EmailDelivery
from app.schemas.email import EmailDeliveryOut, EmailOut

router = APIRouter()


@router.get("/emails", response_model=list[EmailOut])
async def get_emails(
    q: str | None = None,
    sender: str | None = None,
    subject: str | None = None,
    category: str | None = Query(
        None, description="primary | social | promotions | updates | forums"
    ),
    limit: int | None = Query(
        None,
        ge=1,
        description="Maximum results. Omit for no limit (returns everything matching).",
    ),
    db: Session = Depends(get_db)
) -> list[EmailOut]:
    """
    Retrieve emails with optional filtering.

    Query Parameters:
    - q: Search in subject and body
    - sender: Filter by sender email
    - subject: Filter by subject line
    - category: Filter by Gmail category (primary/social/promotions/updates/forums)
    - limit: Maximum results. If omitted, all matching emails are returned.
    """
    try:
        stmt = select(Email).order_by(Email.received_at.desc())

        if limit is not None:
            stmt = stmt.limit(limit)

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

        if category:
            stmt = stmt.where(Email.category == category.lower())

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
    Retrieve a specific email by Gmail message ID (the per-account id
    Gmail assigned to whichever inbox's copy you're looking up).
    """
    try:
        delivery = db.scalar(
            select(EmailDelivery).where(EmailDelivery.gmail_message_id == message_id)
        )

        if not delivery:
            raise HTTPException(
                status_code=404,
                detail=f"Email with message_id '{message_id}' not found"
            )

        return delivery.email

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve email: {str(e)}"
        )


@router.get("/emails/{message_id}/deliveries", response_model=list[EmailDeliveryOut])
async def get_email_deliveries(
    message_id: str,
    db: Session = Depends(get_db)
) -> list[EmailDeliveryOut]:
    """
    Shows every connected inbox that received this email, and how
    (to / cc / bcc). Useful for confirming the same email landed in
    two of your connected accounts without being stored twice.
    """
    delivery = db.scalar(
        select(EmailDelivery).where(EmailDelivery.gmail_message_id == message_id)
    )

    if not delivery:
        raise HTTPException(
            status_code=404,
            detail=f"Email with message_id '{message_id}' not found"
        )

    return delivery.email.deliveries