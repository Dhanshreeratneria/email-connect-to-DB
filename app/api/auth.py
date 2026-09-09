import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.email import GmailAccount
from app.services.gmail_service import gmail, profile
from app.services.oauth_service import (
    authorization_url,
    encrypt_credentials,
    exchange,
)
from app.services.sync_service import initial_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/google")


@router.get("")
def begin():
    """
    Starts Google OAuth authorization.

    Open:
    http://localhost:8000/auth/google
    """
    url, _state = authorization_url()
    return RedirectResponse(url)


@router.get("/callback")
def callback(code: str, db: Session = Depends(get_db)):
    """
    Handles Google OAuth callback.

    Flow:
    Google callback (with authorization code)
        → Exchange code for credentials (PKCE handled by Flow)
        → Get Gmail profile
        → Save encrypted token to database
        → Perform initial Gmail sync
    """

    try:
        logger.info("Starting OAuth callback processing")
        
        # Exchange code for credentials
        # Flow library handles PKCE verification internally
        credentials = exchange(code)
        logger.info("Token exchange successful")
        
        # Get Gmail service
        service = gmail(credentials)
        
        # Get Gmail profile
        gmail_profile = profile(service)
        google_email = gmail_profile["emailAddress"]
        logger.info(f"Got Gmail profile: {google_email}")

        # Check if account already exists
        account = (
            db.query(GmailAccount)
            .filter(GmailAccount.google_email == google_email)
            .first()
        )

        # Encrypt credentials
        encrypted_token = encrypt_credentials(credentials)

        if account is None:
            # Create new account
            account = GmailAccount(
                google_email=google_email,
                encrypted_token=encrypted_token,
            )
            db.add(account)
            logger.info(f"Created new Gmail account: {google_email}")
        else:
            # Update existing account
            account.encrypted_token = encrypted_token
            logger.info(f"Updated Gmail account: {google_email}")

        db.commit()
        db.refresh(account)

        # Perform initial sync
        logger.info(f"Starting initial Gmail sync for {google_email}")
        initial_sync(db, account, service)

        logger.info(
            "Initial Gmail sync completed for account=%s",
            google_email,
        )

        return {
            "status": "connected",
            "google_email": google_email,
            "initial_sync": "completed",
            "gmail_watch": "not enabled yet",
            "next_step": "Configure Pub/Sub, then enable Gmail Watch.",
        }

    except Exception as exc:
        logger.exception("Google OAuth or Gmail initial sync failed")
        
        return {
            "status": "error",
            "message": "Google OAuth callback or initial Gmail sync failed.",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }