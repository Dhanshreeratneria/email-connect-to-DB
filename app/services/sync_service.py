"""
Updated sync service with robust attachment downloading and storage.

Key improvements:
- Automatic attachment downloading during email sync
- Retry logic for failed downloads
- Progress tracking
- Proper error handling
"""

import logging
import time
from typing import Optional, Tuple
from sqlalchemy import select
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
# Google's per-second rate limits.
API_CALL_DELAY = 0.1  # 100ms delay between API calls to avoid rate limits

# Maximum retries for failed attachment downloads
MAX_ATTACHMENT_RETRIES = 3


def store_attachments(
    db: Session,
    email: Email,
    gmail_service,
    gmail_message_id: str,
    attachments_meta: list[dict],
) -> Tuple[int, int]:
    """
    Downloads and stores the actual bytes for any attachment listed in
    Email.attachments (parsed metadata) that isn't already saved in the
    email_attachments table, so PDFs/zips/images/etc. end up persisted
    in the database rather than just their filename/size/mime_type.

    Safe to call every time an already-stored email is seen again
    (e.g. the same email delivered to a second connected account) —
    existing (email_id, gmail_attachment_id) rows are skipped.
    
    Args:
        db: SQLAlchemy session
        email: Email object
        gmail_service: Gmail API service
        gmail_message_id: Gmail message ID
        attachments_meta: List of attachment metadata dicts
        
    Returns:
        Tuple of (successful_downloads, failed_downloads)
    """
    
    successful = 0
    failed = 0

    for meta in attachments_meta:
        attachment_id = meta.get("attachment_id")

        if not attachment_id:
            continue

        # Check if already stored
        already_stored = db.scalar(
            select(EmailAttachment).where(
                EmailAttachment.email_id == email.id,
                EmailAttachment.gmail_attachment_id == attachment_id,
            )
        )

        if already_stored:
            logger.debug(f"Attachment {attachment_id} already stored")
            continue

        # Try to download with retries
        retries = 0
        while retries < MAX_ATTACHMENT_RETRIES:
            try:
                time.sleep(API_CALL_DELAY)
                raw = get_attachment(gmail_service, gmail_message_id, attachment_id)
                content = decode_base64_urlsafe_bytes(raw.get("data", ""))

                # Store in database
                db.add(
                    EmailAttachment(
                        email_id=email.id,
                        gmail_attachment_id=attachment_id,
                        filename=meta.get("filename") or "attachment",
                        mime_type=meta.get("mime_type"),
                        size=raw.get("size") or meta.get("size"),
                        content=content,
                    )
                )
                
                logger.info(
                    f"Stored attachment {meta.get('filename')} ({len(content)} bytes) "
                    f"for email {email.id}"
                )
                
                successful += 1
                break

            except Exception as e:
                retries += 1
                logger.warning(
                    f"Failed to download attachment {attachment_id} (attempt {retries}/{MAX_ATTACHMENT_RETRIES}): {str(e)}"
                )
                
                if retries >= MAX_ATTACHMENT_RETRIES:
                    logger.error(
                        f"Failed to download attachment {attachment_id} for message {gmail_message_id} "
                        f"after {MAX_ATTACHMENT_RETRIES} retries"
                    )
                    failed += 1
                else:
                    time.sleep(1)  # Wait before retry
    
    return successful, failed


def record_email_delivery(
    db: Session,
    account: GmailAccount,
    gmail_message: dict,
    gmail_service=None,
) -> Tuple[Email, int, int]:
    """
    Store (or link to) an email exactly once by its RFC Message-ID header,
    and record how THIS account received it (to / cc / bcc).

    - If this exact account has already recorded this exact Gmail message
      (same account_id + gmail_message_id), nothing changes: returns the
      existing email untouched.
    - If another connected account already stored this same real-world
      email (matched by rfc_message_id), the email content is NOT
      duplicated — only a new EmailDelivery row is added for this account.
    - Otherwise, a brand-new Email row is created.
    
    NEW: Automatically downloads and stores attachment content during sync.

    Returns:
        Tuple of (email, attachments_stored, attachments_failed).
        # BUG FIX: this used to return only `email`. store_attachments()
        # below already computed per-message (successful, failed) counts,
        # but they were silently discarded, so initial_sync()/
        # incremental_sync() could never accumulate real totals and always
        # reported "0 attachments stored / 0 failed" in their stats even
        # when attachments were downloaded successfully.
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
        # BUG FIX: this used to return immediately with (email, 0, 0) and
        # never touch attachments. Any email that was already recorded for
        # this account BEFORE attachment downloading existed (or that was
        # re-delivered to an account that had already seen it) stayed
        # permanently stuck with stored_attachments_count: 0 — there was
        # no code path that ever went back and downloaded its attachment
        # bytes. store_attachments() already de-dupes per
        # (email_id, gmail_attachment_id), so calling it here is safe and
        # only actually hits the Gmail API for attachments still missing.
        email = existing_delivery.email
        stored = failed = 0
        if gmail_service is not None and email.attachments:
            stored, failed = store_attachments(
                db=db,
                email=email,
                gmail_service=gmail_service,
                gmail_message_id=gmail_message_id,
                attachments_meta=email.attachments,
            )
            if stored or failed:
                logger.info(
                    f"Backfilled attachments for already-delivered email "
                    f"{email.id}: {stored} stored, {failed} failed"
                )
        return email, stored, failed

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
        db.flush()  # populate email.id before creating delivery row

    # UPDATED: Download and store attachments
    attachments_stored = 0
    attachments_failed = 0
    if gmail_service is not None and parsed.get("attachments"):
        attachments_stored, attachments_failed = store_attachments(
            db=db,
            email=email,
            gmail_service=gmail_service,
            gmail_message_id=gmail_message_id,
            attachments_meta=parsed["attachments"],
        )
        
        if attachments_stored > 0 or attachments_failed > 0:
            logger.info(
                f"Attachment sync for email {email.id}: "
                f"{attachments_stored} stored, {attachments_failed} failed"
            )

    delivery = EmailDelivery(
        email_id=email.id,
        account_id=account.id,
        gmail_message_id=gmail_message_id,
        delivery_type=delivery_type,
        received_at=parsed["received_at"],
    )
    db.add(delivery)

    return email, attachments_stored, attachments_failed


def backfill_missing_attachments(
    db: Session,
    limit: Optional[int] = None,
) -> dict:
    """
    Downloads attachment bytes for emails that already exist in the
    database, whose metadata (Email.attachments) says they have
    attachments, but whose email_attachments rows are missing or
    incomplete — e.g. emails synced back when attachment downloading
    either didn't exist yet or was skipped because the account's
    delivery row already existed (see the BUG FIX note in
    record_email_delivery above).

    Unlike initial_sync/incremental_sync, this does NOT call Gmail's
    message-list or history APIs — it only re-fetches the specific
    attachment bytes for emails already stored, using each email's
    existing EmailDelivery row to know which connected account's Gmail
    API credentials and gmail_message_id to use.

    Args:
        db: SQLAlchemy session
        limit: Max number of emails to backfill in this call (omit for
               all outstanding emails — useful to call repeatedly / from
               a scheduled job on a large backlog)

    Returns:
        {
            "emails_checked": int,
            "emails_backfilled": list[int],   # Email.id values touched
            "attachments_stored": int,
            "attachments_failed": int,
        }
    """
    from app.services.gmail_service import gmail
    from app.services.oauth_service import decrypt_credentials

    accounts = db.scalars(select(GmailAccount)).all()

    services_by_account_id: dict[int, object] = {}
    for account in accounts:
        try:
            services_by_account_id[account.id] = gmail(
                decrypt_credentials(account.encrypted_token)
            )
        except Exception:
            logger.exception(
                f"backfill_missing_attachments: could not build Gmail "
                f"client for {account.google_email}, skipping its emails"
            )

    stmt = select(Email).where(Email.has_attachments == True)  # noqa: E712
    if limit:
        stmt = stmt.limit(limit)

    emails_checked = 0
    emails_backfilled: list[int] = []
    total_stored = 0
    total_failed = 0

    for email in db.scalars(stmt).all():
        expected = len(email.attachments or [])
        if expected == 0:
            continue

        already_stored = len(email.stored_attachments)
        if already_stored >= expected:
            continue

        emails_checked += 1

        delivery = db.scalar(
            select(EmailDelivery).where(EmailDelivery.email_id == email.id)
        )
        if not delivery or delivery.account_id not in services_by_account_id:
            logger.warning(
                f"backfill_missing_attachments: no usable Gmail "
                f"credentials for email {email.id}, skipping"
            )
            continue

        stored, failed = store_attachments(
            db=db,
            email=email,
            gmail_service=services_by_account_id[delivery.account_id],
            gmail_message_id=delivery.gmail_message_id,
            attachments_meta=email.attachments,
        )
        total_stored += stored
        total_failed += failed
        if stored:
            emails_backfilled.append(email.id)

        db.commit()

    return {
        "emails_checked": emails_checked,
        "emails_backfilled": emails_backfilled,
        "attachments_stored": total_stored,
        "attachments_failed": total_failed,
    }


def initial_sync(
    db: Session,
    account: GmailAccount,
    gmail_service,
) -> dict:
    """
    Fetch the FULL mailbox message list and store every email
    (rate-limited only by a small delay between calls, not by count).

    This runs after initial OAuth authorization or as recovery when a
    stored Gmail History ID becomes invalid.
    
    Returns:
        Dict with sync statistics:
        {
            "emails_imported": int,
            "attachments_stored": int,
            "attachment_failures": int,
            "duration_seconds": float
        }
    """
    
    import time as time_module
    start_time = time_module.time()
    
    imported_count = 0
    total_attachments_stored = 0
    total_attachment_failures = 0
    page_token = None

    try:
        while True:
            logger.info(f"Fetching messages page (imported so far: {imported_count})...")

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
                    _, stored, failed = record_email_delivery(
                        db, account, message, gmail_service
                    )
                    total_attachments_stored += stored
                    total_attachment_failures += failed

                    imported_count += 1

                    if imported_count % 10 == 0:
                        db.commit()
                        logger.info(f"Progress: {imported_count} emails imported")

                except Exception:
                    logger.exception(
                        f"Failed to process message {item.get('id')} during initial sync"
                    )

            # Move to next page
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        db.commit()

        elapsed = time_module.time() - start_time

        logger.info(
            f"Initial sync complete: {imported_count} emails, "
            f"{total_attachments_stored} attachments stored, "
            f"{total_attachment_failures} failures in {elapsed:.1f}s"
        )
        
        return {
            "emails_imported": imported_count,
            "attachments_stored": total_attachments_stored,
            "attachment_failures": total_attachment_failures,
            "duration_seconds": elapsed,
        }

    except Exception:
        logger.exception("Initial sync failed")
        db.rollback()
        raise


def incremental_sync(
    db: Session,
    account: GmailAccount,
    gmail_service,
) -> dict:
    """
    Fetch only new/modified emails since last sync using Gmail History API.

    Much faster than full re-import, but requires maintaining a valid
    history_id in the account record.
    
    Returns:
        Dict with sync statistics (same format as initial_sync)
    """
    
    import time as time_module
    start_time = time_module.time()
    
    if not account.history_id:
        logger.warning(
            f"No history_id for {account.google_email}, falling back to initial sync"
        )
        return initial_sync(db, account, gmail_service)

    imported_count = 0
    total_attachments_stored = 0
    total_attachment_failures = 0
    page_token = None
    start_history_id = account.history_id

    try:
        while True:
            logger.info(f"Fetching history page (processed: {imported_count})...")

            response = history(gmail_service, start_history_id, page_token)
            histories = response.get("history", [])

            if not histories:
                logger.info("No new history to process")
                break

            for hist in histories:
                messages_added = hist.get("messagesAdded", [])

                for item in messages_added:
                    try:
                        time.sleep(API_CALL_DELAY)
                        message = get_message(gmail_service, item["message"]["id"])
                        _, stored, failed = record_email_delivery(
                            db, account, message, gmail_service
                        )
                        total_attachments_stored += stored
                        total_attachment_failures += failed
                        
                        imported_count += 1

                        if imported_count % 10 == 0:
                            db.commit()
                            logger.info(f"Progress: {imported_count} emails synced")

                    except Exception:
                        logger.exception(
                            f"Failed to process message {item['message'].get('id')} "
                            f"during incremental sync"
                        )

            # Update history ID
            new_history_id = response.get("historyId")
            if new_history_id:
                account.history_id = new_history_id

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        db.commit()

        elapsed = time_module.time() - start_time

        logger.info(
            f"Incremental sync complete: {imported_count} emails, "
            f"{total_attachments_stored} attachments stored, "
            f"{total_attachment_failures} failures in {elapsed:.1f}s"
        )
        
        return {
            "emails_imported": imported_count,
            "attachments_stored": total_attachments_stored,
            "attachment_failures": total_attachment_failures,
            "duration_seconds": elapsed,
        }

    except Exception:
        logger.exception("Incremental sync failed")
        db.rollback()
        raise


def watch_mailbox(db: Session, account: GmailAccount, gmail_service, pubsub_topic: str) -> None:
    """
    Set up Gmail push notifications to detect new messages immediately.
    
    Keeps watching until the watch expires (typically 24 hours).
    After expiration, the watch needs to be renewed.
    """
    try:
        logger.info(f"Setting up Gmail watch for {account.google_email}...")
        
        response = watch(gmail_service, pubsub_topic)
        
        if "expiration" in response:
            from datetime import datetime, timezone
            expiration_ms = int(response["expiration"])
            expiration = datetime.fromtimestamp(
                expiration_ms / 1000,
                tz=timezone.utc
            )
            account.watch_expiration = expiration
            
            logger.info(
                f"Gmail watch set up successfully, expires at {expiration.isoformat()}"
            )
        
        db.commit()

    except Exception:
        logger.exception("Failed to set up Gmail watch")
        raise