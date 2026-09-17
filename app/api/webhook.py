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

# Must match the Google Pub/Sub push subscription URL:
# https://YOUR-DOMAIN/webhooks/google/pubsub
router = APIRouter(
    prefix="/webhooks/google",
    tags=["webhook"],
)


def _run_incremental_sync(
    db: Session,
    account: GmailAccount,
    gmail_service,
    notification_history_id: str,
    google_email: str,
) -> None:
    """
    Run Gmail incremental sync in a background worker.

    The Pub/Sub notification history ID is used for logging only.
    incremental_sync() uses account.history_id to determine where
    the previous sync stopped.
    """
    try:
        logger.info(
            "Starting Gmail incremental sync email=%s "
            "notification_history_id=%s account_history_id=%s",
            google_email,
            notification_history_id,
            account.history_id,
        )

        # IMPORTANT:
        # Do NOT pass notification_history_id here.
        imported_count = incremental_sync(
            db=db,
            account=account,
            gmail_service=gmail_service,
        )

        logger.info(
            "Pub/Sub Gmail sync completed email=%s imported=%s history_id=%s",
            google_email,
            imported_count,
            account.history_id,
        )

    except Exception:
        logger.exception(
            "Background Gmail sync failed for %s",
            google_email,
        )


@router.post("/pubsub")
async def gmail_pubsub_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Receive Gmail Watch notifications through Google Cloud Pub/Sub.

    Pub/Sub payload:

    {
        "message": {
            "data": "<base64 encoded JSON>"
        }
    }

    Decoded Gmail notification:

    {
        "emailAddress": "user@gmail.com",
        "historyId": "123456"
    }

    The notification itself does not contain the email.

    We use the stored Gmail OAuth credentials and Gmail History API
    to find newly added messages and save them into PostgreSQL.
    """

    try:
        # ---------------------------------------------------------
        # 1. Read Pub/Sub request
        # ---------------------------------------------------------
        body = await request.json()

        logger.info("Pub/Sub webhook received")

        pubsub_message = body.get("message")

        if not isinstance(pubsub_message, dict):
            raise ValueError(
                "Pub/Sub payload missing message object"
            )

        encoded_data = pubsub_message.get("data")

        if not encoded_data:
            raise ValueError(
                "Pub/Sub message.data is missing"
            )

        # ---------------------------------------------------------
        # 2. Decode Gmail notification
        # ---------------------------------------------------------
        try:
            decoded = base64.b64decode(
                encoded_data,
                validate=True,
            ).decode("utf-8")

            notification = json.loads(decoded)

        except Exception as exc:
            raise ValueError(
                "Invalid Pub/Sub base64/JSON data"
            ) from exc

        google_email = (
            notification.get("emailAddress") or ""
        ).strip().lower()

        notification_history_id = notification.get("historyId")

        if not google_email:
            raise ValueError(
                "Gmail notification missing emailAddress"
            )

        if not notification_history_id:
            raise ValueError(
                "Gmail notification missing historyId"
            )

        logger.info(
            "Gmail notification received email=%s "
            "notification_history_id=%s",
            google_email,
            notification_history_id,
        )

        # ---------------------------------------------------------
        # 3. Find connected Gmail account
        # ---------------------------------------------------------
        account = (
            db.query(GmailAccount)
            .filter(
                GmailAccount.google_email == google_email
            )
            .first()
        )

        if account is None:
            logger.warning(
                "No connected GmailAccount found for %s",
                google_email,
            )

            # Return 200 so Pub/Sub does not retry forever.
            return {
                "status": "ignored",
                "reason": "account_not_connected",
            }

        # ---------------------------------------------------------
        # 4. Decrypt stored Gmail credentials
        # ---------------------------------------------------------
        credentials = decrypt_credentials(
            account.encrypted_token
        )

        gmail_service = gmail(credentials)

        # ---------------------------------------------------------
        # 5. Schedule background Gmail sync
        # ---------------------------------------------------------
        background_tasks.add_task(
            _run_incremental_sync,
            db,
            account,
            gmail_service,
            str(notification_history_id),
            google_email,
        )

        logger.info(
            "Gmail incremental sync scheduled email=%s",
            google_email,
        )

        # ---------------------------------------------------------
        # 6. Immediately acknowledge Pub/Sub
        # ---------------------------------------------------------
        return {
            "status": "accepted",
            "google_email": google_email,
        }

    except Exception as exc:
        logger.exception(
            "Gmail Pub/Sub webhook processing failed"
        )

        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "error": str(exc),
            },
        )