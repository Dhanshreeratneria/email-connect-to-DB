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
def callback(code: str, state: str = None, db: Session = Depends(get_db)):
    """
    Handles Google OAuth callback.

    Flow:
    Google callback
        → Validate state
        → OAuth code exchange with PKCE
        → Gmail profile lookup
        → Encrypted token save in PostgreSQL
        → Initial Gmail import
    """

    try:
        # Exchange code for credentials (with state validation)
        credentials = exchange(code, state)
        service = gmail(credentials)

        gmail_profile = profile(service)
        google_email = gmail_profile["emailAddress"]

        account = (
            db.query(GmailAccount)
            .filter(GmailAccount.google_email == google_email)
            .first()
        )

        encrypted_token = encrypt_credentials(credentials)

        if account is None:
            account = GmailAccount(
                google_email=google_email,
                encrypted_token=encrypted_token,
            )

            db.add(account)
            db.commit()
            db.refresh(account)

        else:
            account.encrypted_token = encrypted_token
            db.commit()
            db.refresh(account)

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

        raise HTTPException(
            status_code=500,
            detail={
                "message": "Google OAuth callback or initial Gmail sync failed.",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        ) from exc