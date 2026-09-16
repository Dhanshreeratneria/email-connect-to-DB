"""Renew Gmail Watch subscriptions that expire within the next 24 hours.

Run this as a scheduled job (for example, every 12 hours on Render Cron).
It must use the same DATABASE_URL and TOKEN_ENCRYPTION_KEY as the web service.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.database import SessionLocal
from app.models.email import GmailAccount
from app.services.gmail_service import gmail
from app.services.oauth_service import decrypt_credentials
from app.services.sync_service import renew_watch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) + timedelta(hours=24)

        accounts = (
            db.query(GmailAccount)
            .filter(
                (GmailAccount.watch_expiration.is_(None))
                | (GmailAccount.watch_expiration < cutoff)
            )
            .all()
        )

        logger.info("Found %s Gmail account(s) requiring watch renewal", len(accounts))

        for account in accounts:
            try:
                credentials = decrypt_credentials(account.encrypted_token)
                service = gmail(credentials)
                result = renew_watch(
                    db=db,
                    account=account,
                    gmail_service=service,
                    pubsub_topic=settings.google_pubsub_topic,
                )
                logger.info(
                    "Gmail Watch renewed account=%s history_id=%s expiration=%s",
                    account.google_email,
                    result.get("historyId"),
                    account.watch_expiration,
                )
            except Exception:
                logger.exception(
                    "Failed to renew Gmail Watch for account=%s",
                    account.google_email,
                )

    finally:
        db.close()


if __name__ == "__main__":
    main()
