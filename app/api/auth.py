import logging

from fastapi import APIRouter, BackgroundTasks, Depends
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
from app.services.sync_service import watch_mailbox, initial_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/google")


@router.get("")
def begin():
    """Starts Google OAuth for manually onboarding a mailbox."""
    url, _state = authorization_url()
    return RedirectResponse(url)


def _finish_account_setup(db: Session, account: GmailAccount, service, google_email: str) -> None:
    """
    Runs the slow parts (full mailbox sync + Gmail watch registration)
    AFTER the redirect back to Claude.ai has already been sent.

    A real mailbox can take well over a minute to fully sync (more if
    Gmail's per-minute rate limit kicks in and forces retries). Running
    that inline, before redirecting, held the browser on this page for
    that whole time. Claude.ai's popup gives up waiting for the redirect
    long before that finishes and shows "Authentication failed" — even
    though the Google login itself had already succeeded. Moving this to
    a background task lets the redirect happen immediately.
    """
    try:
        logger.info(f"Starting initial Gmail sync for {google_email}")
        initial_sync(db, account, service)
        logger.info("Initial Gmail sync completed for account=%s", google_email)
    except Exception:
        logger.exception("Initial Gmail sync failed for account=%s", google_email)
        return

    try:
        logger.info(
            "Creating Gmail Watch account=%s topic=%s",
            google_email,
            settings.google_pubsub_topic,
        )
        watch_mailbox(
            db=db,
            account=account,
            gmail_service=service,
            pubsub_topic=settings.google_pubsub_topic,
        )
        logger.info(
            "Gmail Watch enabled account=%s history_id=%s expiration=%s",
            google_email,
            account.history_id,
            account.watch_expiration,
        )
    except Exception:
        logger.exception(
            "Gmail Watch setup failed for account=%s — account IS connected "
            "and initial sync succeeded, but live sync via Pub/Sub will not "
            "work until this is fixed and the account reconnects.",
            google_email,
        )


@router.get("/callback")
def callback(
    code: str,
    background_tasks: BackgroundTasks,
    state: str = None,
    db: Session = Depends(get_db),
):
    """
    Handles Google OAuth callback for two flows sharing one login screen:
      1. Manual visit to /auth/google -> syncs mailbox, returns JSON.
      2. Claude.ai connector flow (started from /authorize) -> after
         confirming the Google account is the allowed owner, redirects
         back to Claude.ai with an authorization code instead of JSON.

    The redirect back to Claude.ai (or the JSON response for the manual
    flow) is sent BEFORE the mailbox sync runs — see _finish_account_setup.
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

        # Schedule the slow work (full sync + watch registration) to run
        # AFTER this response goes out, instead of blocking the redirect.
        background_tasks.add_task(
            _finish_account_setup, db, account, service, google_email
        )

        if pending is not None:
            auth_code = connector_auth.issue_auth_code(pending, google_email)
            redirect_url = f"{pending['redirect_uri']}?code={auth_code}&state={pending['state']}"
            return RedirectResponse(redirect_url)

        return {
            "status": "connected",
            "google_email": google_email,
            "initial_sync": "running_in_background",
            "note": "Mailbox sync and Gmail watch setup are running in the "
                    "background now; check server logs for progress.",
        }

    except Exception as exc:
        logger.exception("Google OAuth callback failed")
        if pending is not None:
            error_url = f"{pending['redirect_uri']}?error=server_error&state={pending['state']}"
            return RedirectResponse(error_url)
        return {
            "status": "error",
            "message": "Google OAuth callback failed.",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }