import base64
import json
import logging

from fastapi import APIRouter, Request, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.email import GmailAccount
from app.services.gmail_service import gmail
from app.services.oauth_service import decrypt_credentials
from app.services.sync_service import incremental_sync

logger = logging.getLogger(__name__)

# NOTE: this path must exactly match the push endpoint registered on the
# Pub/Sub subscription (see README "Pub/Sub setup") and the
# GOOGLE_PUBSUB_AUDIENCE value in your environment, e.g.
#   https://YOUR-DOMAIN/webhooks/google/pubsub
router = APIRouter(prefix="/webhooks/google", tags=["webhook"])


@router.post("/pubsub")
async def gmail_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receives Pub/Sub push notifications from the Gmail Watch.

    Google sends: {"message": {"data": "<base64 JSON>"}}
    where the decoded JSON is {"emailAddress": "...", "historyId": "..."}.

    This identifies which connected account changed and runs an
    incremental sync so new mail actually lands in Postgres.
    """
    try:
        body = await request.json()
    except Exception:
        logger.exception("Webhook payload was not valid JSON")
        return {"error": "invalid payload"}, 400

    message = body.get("message") or {}
    data_b64 = message.get("data")

    if not data_b64:
        logger.warning("Webhook received with no message.data: %s", body)
        # Still 200 so Pub/Sub doesn't retry a malformed/test message forever.
        return {"status": "ok"}

    try:
        data = json.loads(base64.b64decode(data_b64).decode())
    except Exception:
        logger.exception("Failed to decode Pub/Sub message.data")
        return {"status": "ok"}

    email_address = data.get("emailAddress")
    notification_history_id = data.get("historyId")
    logger.info(
        "Gmail notification for %s (historyId=%s)",
        email_address,
        notification_history_id,
    )

    if not email_address:
        logger.warning("Notification missing emailAddress: %s", data)
        return {"status": "ok"}

    account = (
        db.query(GmailAccount)
        .filter(GmailAccount.google_email == email_address)
        .first()
    )

    if account is None:
        logger.warning("Notification for unknown account: %s", email_address)
        return {"status": "ok"}

    try:
        credentials = decrypt_credentials(account.encrypted_token)
        service = gmail(credentials)

        imported = incremental_sync(
            db=db,
            account=account,
            gmail_service=service,
            notification_history_id=notification_history_id,
        )
        logger.info(
            "Webhook-triggered sync imported %s message(s) for %s",
            imported,
            email_address,
        )
    except Exception:
        # Always return 200 so Pub/Sub doesn't hammer us with retries;
        # the failure is logged for investigation instead.
        logger.exception("Webhook-triggered sync failed for %s", email_address)

    return {"status": "ok"}
