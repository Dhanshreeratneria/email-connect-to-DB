import logging
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.email import Email, EmailAttachment, EmailDelivery, GmailAccount
from app.services.email_parser import decode_base64_urlsafe_bytes, parse_message
from app.services.gmail_service import (
    get_attachment,
    get_message,
    history,
    list_messages,
    profile,
    watch,
)

logger = logging.getLogger(__name__)

# Small delay between individual Gmail API calls purely to stay under
# Google's per-second rate limits. This is NOT a limit on how many
# messages get imported — it just paces the calls.
API_CALL_DELAY = 0.1  # 100ms delay between API calls to avoid rate limits

# Matches the column definitions in EmailAttachment — truncate here so a
# freak long filename/mime_type never raises a DataError at insert time.
MAX_FILENAME_LEN = 1024
MAX_MIME_TYPE_LEN = 255


def store_attachments(
    db: Session,
    email: Email,
    gmail_service,
    gmail_message_id: str,
    attachments_meta: list[dict],
) -> None:
    """
    Downloads and stores the actual bytes for any attachment listed in
    Email.attachments (parsed metadata) that isn't already saved in the
    email_attachments table, so PDFs/zips/images/etc. end up persisted
    in the database rather than just their filename/size/mime_type.

    Safe to call every time an already-stored email is seen again
    (e.g. the same email delivered to a second connected account) —
    existing (email_id, gmail_attachment_id) rows are skipped.

    Every insert runs inside its own SAVEPOINT (db.begin_nested()). If
    one attachment fails to insert — a duplicate slipping in from a
    race condition, a DB constraint error, a connection blip — only
    that savepoint rolls back. The outer transaction (the email +
    delivery rows, plus every other attachment already added) is left
    intact, so one bad attachment can't sink the whole sync batch.
    """

    for meta in attachments_meta:
        attachment_id = meta.get("attachment_id")

        if not attachment_id:
            continue

        already_stored = db.scalar(
            select(EmailAttachment).where(
                EmailAttachment.email_id == email.id,
                EmailAttachment.gmail_attachment_id == attachment_id,
            )
        )

        if already_stored:
            continue

        try:
            time.sleep(API_CALL_DELAY)
            raw = get_attachment(gmail_service, gmail_message_id, attachment_id)
            content = decode_base64_urlsafe_bytes(raw.get("data", ""))

        except Exception:
            logger.exception(
                "Failed to download attachment %s for message %s",
                attachment_id,
                gmail_message_id,
            )
            continue

        filename = (meta.get("filename") or "attachment")[:MAX_FILENAME_LEN]
        mime_type = meta.get("mime_type")

        if mime_type:
            mime_type = mime_type[:MAX_MIME_TYPE_LEN]

        try:
            with db.begin_nested():  # SAVEPOINT — isolates this one insert
                db.add(
                    EmailAttachment(
                        email_id=email.id,
                        gmail_attachment_id=attachment_id,
                        filename=filename,
                        mime_type=mime_type,
                        size=raw.get("size") or meta.get("size"),
                        content=content,
                    )
                )
                db.flush()  # force the insert now, inside the savepoint

        except SQLAlchemyError:
            logger.exception(
                "Failed to store attachment %s for email_id=%s — "
                "skipping just this attachment, rest of the sync continues",
                attachment_id,
                email.id,
            )
            continue


def record_email_delivery(
    db: Session,
    account: GmailAccount,
    gmail_message: dict,
    gmail_service=None,
) -> Email:
    """
    Store (or link to) an email exactly once by its RFC Message-ID header,
    and record how THIS account received it (to / cc / bcc).

    - If this exact account has already recorded this exact Gmail message
      (same account_id + gmail_message_id), nothing changes: returns the
      existing email untouched. This replaces the old account_id+message_id
      "unique protection" behaviour.
    - If another connected account already stored this same real-world
      email (matched by rfc_message_id), the email content is NOT
      duplicated — only a new EmailDelivery row is added for this account.
    - Otherwise, a brand-new Email row is created.
    """

    gmail_message_id = gmail_message["id"]

    # Already recorded for this exact account? Nothing to do.
    existing_delivery = db.scalar(
        select(EmailDelivery).where(
            EmailDelivery.account_id == account.id,
            EmailDelivery.gmail_message_id == gmail_message_id,
        )
    )

    if existing_delivery:
        return existing_delivery.email

    parsed = parse_message(gmail_message)

    # Extra fields used only for delivery-type detection; not DB columns.
    to_addresses = parsed.pop("to_addresses")
    cc_addresses = parsed.pop("cc_addresses")

    account_email = account.google_email.lower()

    if account_email in to_addresses:
        delivery_type = "to"
    elif account_email in cc_addresses:
        delivery_type = "cc"
    else:
        # Gmail never exposes Bcc headers to a recipient's own copy of a
        # message, so "not in To or Cc, but it's in this mailbox" is the
        # only reliable signal that it arrived via Bcc.
        delivery_type = "bcc"

    rfc_message_id = parsed.get("rfc_message_id")
    email = None

    if rfc_message_id:
        email = db.scalar(
            select(Email).where(Email.rfc_message_id == rfc_message_id)
        )

    if email is None:
        email = Email(**parsed)
        db.add(email)
        db.flush()  # populate email.id before creating the delivery row

    if gmail_service is not None and parsed.get("attachments"):
        store_attachments(
            db=db,
            email=email,
            gmail_service=gmail_service,
            gmail_message_id=gmail_message_id,
            attachments_meta=parsed["attachments"],
        )

    delivery = EmailDelivery(
        email_id=email.id,
        account_id=account.id,
        gmail_message_id=gmail_message_id,
        delivery_type=delivery_type,
        received_at=parsed["received_at"],
    )
    db.add(delivery)

    return email


def initial_sync(
    db: Session,
    account: GmailAccount,
    gmail_service,
) -> int:
    """
    Fetch the FULL mailbox message list and store every email
    (rate-limited only by a small delay between calls, not by count).

    This runs after initial OAuth authorization or as recovery when a
    stored Gmail History ID becomes invalid.
    """

    imported_count = 0
    page_token = None

    try:
        while True:
            logger.info(f"Fetching messages page (imported so far: {imported_count})")

            response = list_messages(gmail_service, page_token)
            messages = response.get("messages", [])

            if not messages:
                logger.info("No more messages to fetch")
                break

            for item in messages:
                try:
                    # Small delay to avoid hitting Gmail API rate limits
                    time.sleep(API_CALL_DELAY)

                    message = get_message(gmail_service, item["id"])

                    record_email_delivery(
                        db=db,
                        account=account,
                        gmail_message=message,
                        gmail_service=gmail_service,
                    )

                    imported_count += 1

                except Exception as e:
                    logger.warning(f"Failed to fetch message {item['id']}: {str(e)}")
                    # Continue with next message instead of failing
                    continue

            # Commit after each page
            db.commit()

            page_token = response.get("nextPageToken")

            if not page_token:
                break

        # Get final profile and store history ID (anchors future
        # incremental / pub-sub syncs)
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
    Real-time sync driven by Gmail push notifications (Pub/Sub).

    Uses the Gmail History API to retrieve every new message since the
    last stored history ID, paging through all results with no cap.

    If Gmail rejects an old/expired history ID, caller falls back to a
    full initial_sync.
    """

    if not account.history_id:
        return initial_sync(db, account, gmail_service)

    imported_count = 0
    processed_message_ids: set[str] = set()

    try:
        page_token = None

        while True:
            response = history(
                gmail_service,
                account.history_id,
                page_token=page_token,
            )

            for history_item in response.get("history", []):
                for added in history_item.get("messagesAdded", []):
                    message_id = added["message"]["id"]

                    if message_id in processed_message_ids:
                        continue

                    processed_message_ids.add(message_id)

                    # Small delay to avoid hitting Gmail API rate limits
                    time.sleep(API_CALL_DELAY)

                    message = get_message(gmail_service, message_id)

                    record_email_delivery(
                        db=db,
                        account=account,
                        gmail_message=message,
                        gmail_service=gmail_service,
                    )

                    imported_count += 1

            page_token = response.get("nextPageToken")

            if not page_token:
                # Advance the stored history ID once we've drained every page
                account.history_id = str(
                    notification_history_id
                    or response.get("historyId")
                    or account.history_id
                )
                break

        db.commit()

        logger.info(
            "Incremental Gmail sync completed account=%s imported=%s",
            account.google_email,
            imported_count,
        )

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
    Creates or renews the Gmail Watch that powers real-time Pub/Sub push
    notifications, so incremental_sync() gets triggered as new mail
    arrives instead of relying on polling.

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