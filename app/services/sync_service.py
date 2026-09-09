import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.email import Email, GmailAccount
from app.services.email_parser import parse_message
from app.services.gmail_service import (
    get_message,
    history,
    list_messages,
    profile,
    watch,
)

logger = logging.getLogger(__name__)

# Rate limiting constants
INITIAL_SYNC_MESSAGE_LIMIT = 50  # Fetch only first 50 emails on initial sync
API_CALL_DELAY = 0.1  # 100ms delay between API calls to avoid rate limits


def upsert_email(
    db: Session,
    account: GmailAccount,
    gmail_message: dict,
) -> Email:
    """
    Insert or update a Gmail email.

    Unique protection:
    account_id + message_id
    """

    parsed = parse_message(gmail_message)

    existing = db.scalar(
        select(Email).where(
            Email.account_id == account.id,
            Email.message_id == parsed["message_id"],
        )
    )

    if existing:
        for field_name, field_value in parsed.items():
            setattr(existing, field_name, field_value)

        return existing

    email = Email(
        account_id=account.id,
        **parsed,
    )

    db.add(email)

    return email


def initial_sync(
    db: Session,
    account: GmailAccount,
    gmail_service,
) -> int:
    """
    Fetch the mailbox message list and store emails (with rate limiting).

    This runs after initial OAuth authorization or as recovery when a
    stored Gmail History ID becomes invalid.
    
    Note: Limited to first 50 emails to avoid rate limit issues.
    Future syncs use incremental_sync() which is more efficient.
    """

    imported_count = 0
    page_token = None
    total_fetched = 0

    try:
        while total_fetched < INITIAL_SYNC_MESSAGE_LIMIT:
            logger.info(f"Fetching messages page (imported so far: {imported_count})")
            
            response = list_messages(gmail_service, page_token)
            messages = response.get("messages", [])
            
            if not messages:
                logger.info("No more messages to fetch")
                break

            for item in messages:
                if total_fetched >= INITIAL_SYNC_MESSAGE_LIMIT:
                    logger.info(f"Reached initial sync limit of {INITIAL_SYNC_MESSAGE_LIMIT} messages")
                    break
                
                try:
                    # Add delay to avoid rate limiting
                    time.sleep(API_CALL_DELAY)
                    
                    message = get_message(gmail_service, item["id"])

                    upsert_email(
                        db=db,
                        account=account,
                        gmail_message=message,
                    )

                    imported_count += 1
                    total_fetched += 1
                    
                except Exception as e:
                    logger.warning(f"Failed to fetch message {item['id']}: {str(e)}")
                    # Continue with next message instead of failing
                    continue

            # Commit after each page
            db.commit()
            
            page_token = response.get("nextPageToken")

            if not page_token or total_fetched >= INITIAL_SYNC_MESSAGE_LIMIT:
                break

        # Get final profile and store history ID
        mailbox_profile = profile(gmail_service)
        account.history_id = str(mailbox_profile["historyId"])
        db.commit()

        logger.info(
            "Initial Gmail sync completed account=%s imported=%s",
            account.google_email,
            imported_count,
        )

        return imported_count
        
    except Exception as exc:
        logger.exception(f"Initial sync failed for account {account.google_email}: {str(exc)}")
        # Don't re-raise - let callback handle it
        raise


def incremental_sync(
    db: Session,
    account: GmailAccount,
    gmail_service,
    notification_history_id: str | None = None,
) -> int:
    """
    Uses Gmail History API to retrieve new Gmail messages since the last
    stored history ID.

    If Gmail rejects an old history ID, caller should run initial_sync.
    """

    if not account.history_id:
        return initial_sync(db, account, gmail_service)

    imported_count = 0
    processed_message_ids: set[str] = set()

    try:
        response = history(
            gmail_service,
            account.history_id,
        )

        while True:
            for history_item in response.get("history", []):
                for added in history_item.get("messagesAdded", []):
                    message_id = added["message"]["id"]

                    if message_id in processed_message_ids:
                        continue

                    processed_message_ids.add(message_id)
                    
                    # Add small delay
                    time.sleep(API_CALL_DELAY)

                    message = get_message(gmail_service, message_id)

                    upsert_email(
                        db=db,
                        account=account,
                        gmail_message=message,
                    )

                    imported_count += 1

            next_page_token = response.get("nextPageToken")

            if not next_page_token:
                break

            response = gmail_service.users().history().list(
                userId="me",
                startHistoryId=account.history_id,
                historyTypes=["messageAdded"],
                pageToken=next_page_token,
            ).execute()

        account.history_id = str(
            notification_history_id
            or response.get("historyId")
            or account.history_id
        )

        db.commit()

        return imported_count

    except Exception:
        logger.exception(
            "Gmail History API sync failed; starting full resync "
            "account=%s",
            account.google_email,
        )

        return initial_sync(db, account, gmail_service)


def create_or_renew_watch(
    db: Session,
    account: GmailAccount,
    gmail_service,
    pubsub_topic: str,
) -> dict:
    """
    Creates or renews Gmail Watch.

    Requires:
    - Gmail API enabled
    - Pub/Sub topic created
    - gmail-api-push@system.gserviceaccount.com granted Pub/Sub Publisher
    """

    result = watch(gmail_service, pubsub_topic)

    account.history_id = str(result["historyId"])

    if result.get("expiration"):
        from datetime import datetime, timezone

        account.watch_expiration = datetime.fromtimestamp(
            int(result["expiration"]) / 1000,
            tz=timezone.utc,
        )

    db.commit()

    return result



def incremental_sync(
    db: Session,
    account: GmailAccount,
    gmail_service,
    notification_history_id: str | None = None,
) -> int:
    """
    Uses Gmail History API to retrieve new Gmail messages since the last
    stored history ID.

    If Gmail rejects an old history ID, caller should run initial_sync.
    """

    if not account.history_id:
        return initial_sync(db, account, gmail_service)

    imported_count = 0
    processed_message_ids: set[str] = set()

    try:
        response = history(
            gmail_service,
            account.history_id,
        )

        while True:
            for history_item in response.get("history", []):
                for added in history_item.get("messagesAdded", []):
                    message_id = added["message"]["id"]

                    if message_id in processed_message_ids:
                        continue

                    processed_message_ids.add(message_id)

                    message = get_message(gmail_service, message_id)

                    upsert_email(
                        db=db,
                        account=account,
                        gmail_message=message,
                    )

                    imported_count += 1

            next_page_token = response.get("nextPageToken")

            if not next_page_token:
                break

            response = gmail_service.users().history().list(
                userId="me",
                startHistoryId=account.history_id,
                historyTypes=["messageAdded"],
                pageToken=next_page_token,
            ).execute()

        account.history_id = str(
            notification_history_id
            or response.get("historyId")
            or account.history_id
        )

        db.commit()

        return imported_count

    except Exception:
        logger.exception(
            "Gmail History API sync failed; starting full resync "
            "account=%s",
            account.google_email,
        )

        return initial_sync(db, account, gmail_service)


def create_or_renew_watch(
    db: Session,
    account: GmailAccount,
    gmail_service,
    pubsub_topic: str,
) -> dict:
    """
    Creates or renews Gmail Watch.

    Requires:
    - Gmail API enabled
    - Pub/Sub topic created
    - gmail-api-push@system.gserviceaccount.com granted Pub/Sub Publisher
    """

    result = watch(gmail_service, pubsub_topic)

    account.history_id = str(result["historyId"])

    if result.get("expiration"):
        from datetime import datetime, timezone

        account.watch_expiration = datetime.fromtimestamp(
            int(result["expiration"]) / 1000,
            tz=timezone.utc,
        )

    db.commit()

    return result