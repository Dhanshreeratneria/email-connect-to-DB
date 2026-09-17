import base64
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.email import GmailAccount
from app.services.gmail_service import gmail
from app.services.oauth_service import decrypt_credentials
from app.services.sync_service import incremental_sync

logger = logging.getLogger(__name__)

# This path must match the Google Pub/Sub push subscription URL.
router = APIRouter(prefix="/webhooks/google", tags=["webhook"])


def _run_incremental_sync(
    db: Session,
    account: GmailAccount,
    gmail_service,
    notification_history_id: str,
    google_email: str,
) -> None:
    """
    Runs the (blocking) sync off the request/response cycle.
    FastAPI executes sync background tasks in a worker thread, so this
    no longer freezes the single event loop (and the /mcp endpoint)
    while a full resync grinds through the mailbox.
    """
    try:
        imported_count = incremental_sync(
            db=db,
            account=account,
            gmail_service=gmail_service,
            notification_history_id=notification_history_id,
        )
        logger.info(
            "Pub/Sub Gmail sync completed email=%s imported=%s history_id=%s",
            google_email,
            imported_count,
            account.history_id,
        )
    except Exception:
        logger.exception("Background Gmail sync failed for %s", google_email)


@router.post("/pubsub")
async def gmail_pubsub_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Receives Gmail Watch notifications through Google Cloud Pub/Sub.

    Pub/Sub sends a payload like:
    {
        "message": {
            "data": "<base64 JSON>"
        }
    }

    The decoded Gmail notification contains:
    {
        "emailAddress": "user@gmail.com",
        "historyId": "123456"
    }

    The notification does not contain the email itself. We use the stored
    Gmail account credentials + Gmail History API to fetch the new messages,
    then incremental_sync() stores them in PostgreSQL.
    """
    try:
        body = await request.json()
        logger.info("Pub/Sub webhook received")

        pubsub_message = body.get("message")
        if not isinstance(pubsub_message, dict):
            raise ValueError("Pub/Sub payload missing message object")

        encoded_data = pubsub_message.get("data")
        if not encoded_data:
            raise ValueError("Pub/Sub message.data is missing")

        try:
            decoded = base64.b64decode(encoded_data, validate=True).decode("utf-8")
            notification = json.loads(decoded)
        except Exception as exc:
            raise ValueError("Invalid Pub/Sub base64/JSON data") from exc

        google_email = (notification.get("emailAddress") or "").strip().lower()
        notification_history_id = notification.get("historyId")

        if not google_email:
            raise ValueError("Gmail notification missing emailAddress")
        if not notification_history_id:
            raise ValueError("Gmail notification missing historyId")

        logger.info(
            "Gmail notification received email=%s notification_history_id=%s",
            google_email,
            notification_history_id,
        )

        account = (
            db.query(GmailAccount)
            .filter(GmailAccount.google_email == google_email)
            .first()
        )

        if account is None:
            logger.warning("No connected GmailAccount found for %s", google_email)
            # Return 200 so Pub/Sub does not retry forever for an account that
            # is intentionally not connected to this application.
            return {"status": "ignored", "reason": "account_not_connected"}

        # Rebuild the Gmail API credentials from the encrypted token stored in
        # PostgreSQL. No Gmail credential is sent by Pub/Sub itself.
        credentials = decrypt_credentials(account.encrypted_token)
        gmail_service = gmail(credentials)

        # Schedule the sync to run AFTER this response is sent, in a
        # worker thread, instead of blocking this (only) event loop.
        # This is also what Pub/Sub push expects: ack quickly, or it
        # will keep re-delivering the notification.
        background_tasks.add_task(
            _run_incremental_sync,
            db,
            account,
            gmail_service,
            str(notification_history_id),
            google_email,
        )

        return {
            "status": "accepted",
            "google_email": google_email,
        }

    except Exception as exc:
        logger.exception("Gmail Pub/Sub webhook processing failed")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "error": str(exc),
            },
        )