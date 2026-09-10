import logging

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.email import GmailAccount
from app.services import connector_auth
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
    """Starts Google OAuth for manually onboarding a mailbox."""
    url, _state = authorization_url()
    return RedirectResponse(url)


@router.get("/callback")
def callback(code: str, state: str = None, db: Session = Depends(get_db)):
    """
    Handles Google OAuth callback for two flows sharing one login screen:
      1. Manual visit to /auth/google -> syncs mailbox, returns JSON.
      2. Claude.ai connector flow (started from /authorize) -> after
         confirming the Google account is the allowed owner, redirects
         back to Claude.ai with an authorization code instead of JSON.
    """
    pending = connector_auth.resolve_google_state(state) if state else None

    try:
        logger.info("Starting OAuth callback processing")

        credentials = exchange(code, state)
        logger.info("Token exchange successful")

        service = gmail(credentials)
        gmail_profile = profile(service)
        google_email = gmail_profile["emailAddress"]
        logger.info(f"Got Gmail profile: {google_email}")

        if pending is not None and google_email != settings.connector_allowed_email:
            logger.warning("Connector login rejected for %s", google_email)
            denial_url = f"{pending['redirect_uri']}?error=access_denied&state={pending['state']}"
            return RedirectResponse(denial_url)

        account = (
            db.query(GmailAccount)
            .filter(GmailAccount.google_email == google_email)
            .first()
        )

        encrypted_token = encrypt_credentials(credentials)

        if account is None:
            account = GmailAccount(google_email=google_email, encrypted_token=encrypted_token)
            db.add(account)
            logger.info(f"Created new Gmail account: {google_email}")
        else:
            account.encrypted_token = encrypted_token
            logger.info(f"Updated Gmail account: {google_email}")

        db.commit()
        db.refresh(account)

        logger.info(f"Starting initial Gmail sync for {google_email}")
        initial_sync(db, account, service)
        logger.info("Initial Gmail sync completed for account=%s", google_email)

        if pending is not None:
            auth_code = connector_auth.issue_auth_code(pending, google_email)
            redirect_url = f"{pending['redirect_uri']}?code={auth_code}&state={pending['state']}"
            return RedirectResponse(redirect_url)

        return {
            "status": "connected",
            "google_email": google_email,
            "initial_sync": "completed",
            "gmail_watch": "not enabled yet",
            "next_step": "Configure Pub/Sub, then enable Gmail Watch.",
        }

    except Exception as exc:
        logger.exception("Google OAuth or Gmail initial sync failed")
        if pending is not None:
            error_url = f"{pending['redirect_uri']}?error=server_error&state={pending['state']}"
            return RedirectResponse(error_url)
        return {
            "status": "error",
            "message": "Google OAuth callback or initial Gmail sync failed.",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
