from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.email import (
    Email,
    EmailAttachment,
    EmailDelivery,
    GmailAccount,
)
from app.services.email_parser import (
    decode_base64_urlsafe_bytes,
    parse_message,
)
from app.services.gmail_service import (
    get_attachment,
    get_message,
    history,
    list_messages,
    profile,
    watch,
)

logger = logging.getLogger(__name__)


# ============================================================
# ATTACHMENT STORAGE
# ============================================================


def store_attachments(
    db: Session,
    email: Email,
    gmail_service: Any,
    gmail_message_id: str,
    attachments_meta: list[dict[str, Any]],
) -> Tuple[int, int]:
    """
    Download Gmail attachments and store their binary content
    in PostgreSQL email_attachments.content.

    Returns:
        (successful_count, failed_count)

    Important:
        - Existing attachments with content are skipped.
        - Existing attachments with NULL content are retried.
        - Duplicate attachment inserts use a SAVEPOINT so that
          one duplicate cannot rollback the whole transaction.
    """

    successful = 0
    failed = 0

    if not attachments_meta:
        return successful, failed

    for meta in attachments_meta:
        attachment_id = meta.get("attachment_id")

        if not attachment_id:
            logger.warning(
                "Skipping attachment without attachment_id for email %s",
                email.id,
            )
            failed += 1
            continue

        filename = meta.get("filename") or "unknown"
        mime_type = meta.get("mime_type")
        size = meta.get("size")

        try:
            # ------------------------------------------------
            # Check whether attachment already exists
            # ------------------------------------------------
            existing = db.scalar(
                select(EmailAttachment).where(
                    EmailAttachment.email_id == email.id,
                    EmailAttachment.gmail_attachment_id == attachment_id,
                )
            )

            # Already downloaded successfully.
            if existing is not None and existing.content is not None:
                logger.info(
                    "Attachment already stored: %s for email %s",
                    filename,
                    email.id,
                )
                successful += 1
                continue

            # ------------------------------------------------
            # Download attachment from Gmail
            # ------------------------------------------------
            raw = get_attachment(
                gmail_service,
                gmail_message_id,
                attachment_id,
            )

            if not raw:
                raise ValueError(
                    f"Gmail returned no attachment data for {filename}"
                )

            encoded_data = raw.get("data")

            if not encoded_data:
                raise ValueError(
                    f"Gmail attachment response contains no data: {filename}"
                )

            content = decode_base64_urlsafe_bytes(encoded_data)

            if not content:
                raise ValueError(
                    f"Decoded attachment is empty: {filename}"
                )

            # ------------------------------------------------
            # Existing row but content was NULL -> retry/update
            # ------------------------------------------------
            if existing is not None:
                existing.filename = filename
                existing.mime_type = mime_type
                existing.size = size
                existing.content = content

                db.flush()

                logger.info(
                    "Retried attachment %s (%d bytes) for email %s",
                    filename,
                    len(content),
                    email.id,
                )

                successful += 1
                continue

            # ------------------------------------------------
            # Create new attachment row
            # ------------------------------------------------
            attachment = EmailAttachment(
                email_id=email.id,
                gmail_attachment_id=attachment_id,
                filename=filename,
                mime_type=mime_type,
                size=size,
                content=content,
            )

            # SAVEPOINT:
            # If another Pub/Sub worker inserts the same attachment
            # at the same time, only this INSERT is rolled back.
            try:
                with db.begin_nested():
                    db.add(attachment)
                    db.flush()

                logger.info(
                    "Stored attachment %s (%d bytes) for email %s",
                    filename,
                    len(content),
                    email.id,
                )

                successful += 1

            except IntegrityError:
                # Another worker probably inserted it first.
                existing_after_race = db.scalar(
                    select(EmailAttachment).where(
                        EmailAttachment.email_id == email.id,
                        EmailAttachment.gmail_attachment_id == attachment_id,
                    )
                )

                if (
                    existing_after_race is not None
                    and existing_after_race.content is not None
                ):
                    logger.info(
                        "Attachment already stored by another sync: %s "
                        "for email %s",
                        filename,
                        email.id,
                    )
                    successful += 1
                else:
                    raise

        except Exception:
            failed += 1

            logger.exception(
                "Failed to store attachment %s for email %s",
                filename,
                email.id,
            )

    return successful, failed


# ============================================================
# DELIVERY HELPERS
# ============================================================


def _get_existing_delivery(
    db: Session,
    account: GmailAccount,
    gmail_message_id: str,
) -> Optional[EmailDelivery]:
    """
    Find an already-recorded Gmail delivery.

    This is the first idempotency check and prevents the same
    Pub/Sub notification from creating another delivery.
    """

    return db.scalar(
        select(EmailDelivery)
        .where(
            EmailDelivery.account_id == account.id,
            EmailDelivery.gmail_message_id == gmail_message_id,
        )
        .limit(1)
    )


def _get_or_create_email(
    db: Session,
    parsed: dict[str, Any],
) -> Email:
    """
    Get the canonical Email row using RFC Message-ID.

    Email.rfc_message_id represents the real-world email and is
    therefore different from Gmail's per-account message ID.
    """

    rfc_message_id = parsed.get("rfc_message_id")

    # --------------------------------------------------------
    # Existing email by RFC Message-ID
    # --------------------------------------------------------
    if rfc_message_id:
        existing = db.scalar(
            select(Email)
            .where(Email.rfc_message_id == rfc_message_id)
            .limit(1)
        )

        if existing is not None:
            return existing

    # --------------------------------------------------------
    # No existing email -> create it
    # --------------------------------------------------------
    email = Email(**parsed)

    db.add(email)

    try:
        # Flush so email.id is available for EmailDelivery and
        # EmailAttachment.
        with db.begin_nested():
            db.flush()

        return email

    except IntegrityError:
        # Another concurrent sync may have inserted the same
        # RFC Message-ID.
        if rfc_message_id:
            existing = db.scalar(
                select(Email)
                .where(Email.rfc_message_id == rfc_message_id)
                .limit(1)
            )

            if existing is not None:
                return existing

        raise


def _determine_delivery_type(
    account: GmailAccount,
    parsed: dict[str, Any],
) -> str:
    """
    Determine whether the connected Gmail account received the
    email as To, Cc or Bcc.
    """

    account_email = account.google_email.lower().strip()

    to_addresses = {
        str(value).lower().strip()
        for value in parsed.get("to_addresses", [])
        if value
    }

    cc_addresses = {
        str(value).lower().strip()
        for value in parsed.get("cc_addresses", [])
        if value
    }

    if account_email in to_addresses:
        return "to"

    if account_email in cc_addresses:
        return "cc"

    return "bcc"


def _merge_attachment_metadata(
    email: Email,
    parsed_attachments: list[dict[str, Any]],
) -> None:
    """
    Keep Email.attachments JSON metadata synchronized with the
    Gmail payload.

    Actual binary content is stored separately in
    EmailAttachment.content.
    """

    if not parsed_attachments:
        return

    existing_metadata = email.attachments or []

    # Make a lookup by Gmail attachment ID.
    existing_by_id = {
        item.get("attachment_id"): item
        for item in existing_metadata
        if isinstance(item, dict) and item.get("attachment_id")
    }

    for item in parsed_attachments:
        attachment_id = item.get("attachment_id")

        if attachment_id:
            existing_by_id[attachment_id] = item

    email.attachments = list(existing_by_id.values())
    email.has_attachments = len(email.attachments) > 0


def record_email_delivery(
    db: Session,
    account: GmailAccount,
    message: dict[str, Any],
    gmail_service: Any,
    attachment_stats: Optional[dict[str, int]] = None,
) -> EmailDelivery:
    """
    Process one Gmail message.

    Flow:

        Gmail message
             ↓
        parse_message()
             ↓
        find/create canonical Email
             ↓
        find/create EmailDelivery
             ↓
        download attachments
             ↓
        store attachment bytes in PostgreSQL

    The function is idempotent and safe to call repeatedly for the
    same Gmail message.
    """

    gmail_message_id = message.get("id")

    if not gmail_message_id:
        raise ValueError("Gmail message does not contain an id")

    # --------------------------------------------------------
    # STEP 1: Delivery already exists
    # --------------------------------------------------------
    existing_delivery = _get_existing_delivery(
        db,
        account,
        gmail_message_id,
    )

    if existing_delivery is not None:
        logger.info(
            "Delivery already exists for Gmail message %s "
            "(email_id=%s). Retrying/checking attachments.",
            gmail_message_id,
            existing_delivery.email_id,
        )

        email = existing_delivery.email

        # If relationship is not loaded, fetch it explicitly.
        if email is None:
            email = db.get(Email, existing_delivery.email_id)

        if email is None:
            raise RuntimeError(
                f"Delivery {existing_delivery.id} points to missing "
                f"email {existing_delivery.email_id}"
            )

        parsed = parse_message(message)

        attachments_meta = parsed.get("attachments") or []

        successful, failed = store_attachments(
            db=db,
            email=email,
            gmail_service=gmail_service,
            gmail_message_id=gmail_message_id,
            attachments_meta=attachments_meta,
        )

        if attachment_stats is not None:
            attachment_stats["successful"] += successful
            attachment_stats["failed"] += failed

        return existing_delivery

    # --------------------------------------------------------
    # STEP 2: Parse Gmail message
    # --------------------------------------------------------
    parsed = parse_message(message)

    # These are helper fields and are NOT columns in Email.
    to_addresses = parsed.pop("to_addresses", [])
    cc_addresses = parsed.pop("cc_addresses", [])

    attachments_meta = parsed.get("attachments") or []

    # --------------------------------------------------------
    # STEP 3: Get/create canonical Email
    # --------------------------------------------------------
    email = _get_or_create_email(
        db,
        parsed,
    )

    # Keep JSON attachment metadata updated.
    _merge_attachment_metadata(
        email,
        attachments_meta,
    )

    # --------------------------------------------------------
    # STEP 4: Determine To/Cc/Bcc
    # --------------------------------------------------------
    delivery_type = _determine_delivery_type(
        account,
        {
            "to_addresses": to_addresses,
            "cc_addresses": cc_addresses,
        },
    )

    # --------------------------------------------------------
    # STEP 5: Create delivery
    # --------------------------------------------------------
    received_at = email.received_at or datetime.now(timezone.utc)

    delivery = EmailDelivery(
        email_id=email.id,
        account_id=account.id,
        gmail_message_id=gmail_message_id,
        delivery_type=delivery_type,
        received_at=received_at,
    )

    try:
        # SAVEPOINT prevents a duplicate delivery from rolling
        # back the Email/attachment transaction.
        with db.begin_nested():
            db.add(delivery)
            db.flush()

        logger.info(
            "Recorded email delivery: gmail_message_id=%s "
            "email_id=%s account_id=%s type=%s",
            gmail_message_id,
            email.id,
            account.id,
            delivery_type,
        )

    except IntegrityError:
        # Another Pub/Sub worker inserted the delivery first.
        existing_delivery = _get_existing_delivery(
            db,
            account,
            gmail_message_id,
        )

        if existing_delivery is None:
            raise

        logger.info(
            "Duplicate delivery detected for Gmail message %s; "
            "using existing delivery id=%s",
            gmail_message_id,
            existing_delivery.id,
        )

        # Use the canonical email referenced by the winning delivery.
        canonical_email = db.get(
            Email,
            existing_delivery.email_id,
        )

        if canonical_email is None:
            raise RuntimeError(
                f"Existing delivery {existing_delivery.id} references "
                f"missing email {existing_delivery.email_id}"
            )

        email = canonical_email
        delivery = existing_delivery

    # --------------------------------------------------------
    # STEP 6: Store actual attachment bytes
    # --------------------------------------------------------
    successful, failed = store_attachments(
        db=db,
        email=email,
        gmail_service=gmail_service,
        gmail_message_id=gmail_message_id,
        attachments_meta=attachments_meta,
    )

    if attachment_stats is not None:
        attachment_stats["successful"] += successful
        attachment_stats["failed"] += failed

    logger.info(
        "Attachment sync for email %s: %d stored, %d failed",
        email.id,
        successful,
        failed,
    )

    return delivery


# ============================================================
# INITIAL SYNC
# ============================================================


def initial_sync(
    db: Session,
    account: GmailAccount,
    gmail_service: Any,
    max_results: int = 100,
) -> dict[str, Any]:
    """
    Perform the first/full Gmail mailbox synchronization.
    """

    logger.info(
        "Starting initial sync for Gmail account %s",
        account.google_email,
    )

    stats = {
        "emails": 0,
        "attachments_stored": 0,
        "attachment_failures": 0,
        "failures": 0,
    }

    try:
        messages = list_messages(
            gmail_service,
            max_results=max_results,
        )

        if not messages:
            logger.info(
                "Initial sync: no Gmail messages found for %s",
                account.google_email,
            )

            db.commit()

            return stats

        for message_item in messages:
            gmail_message_id = (
                message_item.get("id")
                if isinstance(message_item, dict)
                else None
            )

            if not gmail_message_id:
                continue

            try:
                # Fetch complete message payload.
                message = get_message(
                    gmail_service,
                    gmail_message_id,
                )

                attachment_stats = {
                    "successful": 0,
                    "failed": 0,
                }

                record_email_delivery(
                    db=db,
                    account=account,
                    message=message,
                    gmail_service=gmail_service,
                    attachment_stats=attachment_stats,
                )

                stats["emails"] += 1
                stats["attachments_stored"] += attachment_stats["successful"]
                stats["attachment_failures"] += attachment_stats["failed"]

                # Commit each message so one bad message does not
                # rollback successfully synchronized previous messages.
                db.commit()

            except Exception:
                db.rollback()

                stats["failures"] += 1

                logger.exception(
                    "Initial sync failed for Gmail message %s",
                    gmail_message_id,
                )

        logger.info(
            "Initial sync complete: %d emails, %d attachments stored, "
            "%d attachment failures, %d message failures",
            stats["emails"],
            stats["attachments_stored"],
            stats["attachment_failures"],
            stats["failures"],
        )

        return stats

    except Exception:
        db.rollback()

        logger.exception(
            "Initial sync failed for account %s",
            account.google_email,
        )

        raise


# ============================================================
# INCREMENTAL SYNC
# ============================================================


def incremental_sync(
    db: Session,
    account: GmailAccount,
    gmail_service: Any,
    notification_history_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Process Gmail History API changes after the last known history ID.

    This function is safe to run multiple times because:
        - Email is deduplicated by RFC Message-ID.
        - EmailDelivery is deduplicated by account + Gmail message ID.
        - EmailAttachment is deduplicated by email + attachment ID.
    """

    logger.info(
        "Starting incremental sync for account %s",
        account.google_email,
    )

    stats = {
        "emails": 0,
        "attachments_stored": 0,
        "attachment_failures": 0,
        "failures": 0,
    }

    # --------------------------------------------------------
    # Determine starting history ID
    # --------------------------------------------------------
    start_history_id = (
        notification_history_id
        or account.history_id
    )

    if not start_history_id:
        logger.warning(
            "No history_id available for account %s; "
            "falling back to initial sync.",
            account.google_email,
        )

        return initial_sync(
            db=db,
            account=account,
            gmail_service=gmail_service,
        )

    try:
        history_response = history(
            gmail_service,
            start_history_id,
        )

        if not history_response:
            logger.info(
                "No history response for account %s",
                account.google_email,
            )

            db.commit()
            return stats

        # ----------------------------------------------------
        # Gmail History API response
        # ----------------------------------------------------
        history_items = history_response.get("history") or []

        # Gmail returns a new historyId in the response.
        new_history_id = history_response.get("historyId")

        processed_message_ids: set[str] = set()

        # ----------------------------------------------------
        # Collect message IDs
        # ----------------------------------------------------
        for history_item in history_items:

            # messagesAdded
            for added in history_item.get("messagesAdded", []) or []:
                message_info = added.get("message") or {}

                message_id = message_info.get("id")

                if message_id:
                    processed_message_ids.add(message_id)

            # Some Gmail responses may expose messages directly.
            for message_info in history_item.get("messages", []) or []:
                message_id = message_info.get("id")

                if message_id:
                    processed_message_ids.add(message_id)

        logger.info(
            "Incremental sync found %d Gmail messages",
            len(processed_message_ids),
        )

        # ----------------------------------------------------
        # Process each message
        # ----------------------------------------------------
        for gmail_message_id in processed_message_ids:

            try:
                message = get_message(
                    gmail_service,
                    gmail_message_id,
                )

                attachment_stats = {
                    "successful": 0,
                    "failed": 0,
                }

                record_email_delivery(
                    db=db,
                    account=account,
                    message=message,
                    gmail_service=gmail_service,
                    attachment_stats=attachment_stats,
                )

                stats["emails"] += 1
                stats["attachments_stored"] += attachment_stats["successful"]
                stats["attachment_failures"] += attachment_stats["failed"]

                # Commit successful message immediately.
                db.commit()

            except Exception:
                db.rollback()

                stats["failures"] += 1

                logger.exception(
                    "Incremental sync failed for Gmail message %s",
                    gmail_message_id,
                )

        # ----------------------------------------------------
        # Update Gmail history cursor
        # ----------------------------------------------------
        if new_history_id:
            account.history_id = str(new_history_id)
            db.commit()

        logger.info(
            "Incremental sync complete: %d emails, %d attachments stored, "
            "%d attachment failures, %d message failures",
            stats["emails"],
            stats["attachments_stored"],
            stats["attachment_failures"],
            stats["failures"],
        )

        return stats

    except Exception:
        db.rollback()

        logger.exception(
            "Incremental sync failed for account %s",
            account.google_email,
        )

        raise


# ============================================================
# WATCH / PUBSUB
# ============================================================


def watch_mailbox(
    db: Session,
    account: GmailAccount,
    gmail_service: Any,
) -> dict[str, Any]:
    """
    Register Gmail push notifications through Google Pub/Sub.
    """

    logger.info(
        "Starting Gmail watch for account %s",
        account.google_email,
    )

    result = watch(
        gmail_service,
    )

    if not result:
        raise RuntimeError(
            "Gmail watch() returned an empty response"
        )

    history_id = result.get("historyId")
    expiration = result.get("expiration")

    if history_id:
        account.history_id = str(history_id)

    if expiration:
        try:
            # Gmail expiration is milliseconds since epoch.
            account.watch_expiration = datetime.fromtimestamp(
                int(expiration) / 1000,
                tz=timezone.utc,
            )
        except (TypeError, ValueError, OverflowError):
            logger.warning(
                "Could not parse Gmail watch expiration: %s",
                expiration,
            )

    db.commit()

    logger.info(
        "Gmail watch registered for %s: history_id=%s expiration=%s",
        account.google_email,
        history_id,
        expiration,
    )

    return result


# ============================================================
# RETRY ATTACHMENTS
# ============================================================


def retry_missing_attachments(
    db: Session,
    account: GmailAccount,
    gmail_service: Any,
    limit: int = 100,
) -> dict[str, int]:
    """
    Retry attachments that have metadata but whose binary content
    has not yet been stored in PostgreSQL.

    Useful when:
        - Gmail API temporarily failed
        - Pub/Sub workers overlapped
        - attachment download failed
        - previous transaction was rolled back
    """

    stats = {
        "emails": 0,
        "attachments_stored": 0,
        "failures": 0,
    }

    rows = db.execute(
        select(
            EmailAttachment,
            EmailDelivery,
        )
        .join(
            Email,
            Email.id == EmailAttachment.email_id,
        )
        .join(
            EmailDelivery,
            EmailDelivery.email_id == Email.id,
        )
        .where(
            EmailDelivery.account_id == account.id,
            EmailAttachment.content.is_(None),
        )
        .limit(limit)
    ).all()

    logger.info(
        "Attachment retry found %d missing attachments",
        len(rows),
    )

    for attachment, delivery in rows:
        try:
            raw = get_attachment(
                gmail_service,
                delivery.gmail_message_id,
                attachment.gmail_attachment_id,
            )

            if not raw or not raw.get("data"):
                raise ValueError(
                    f"No data returned for attachment {attachment.filename}"
                )

            content = decode_base64_urlsafe_bytes(
                raw["data"]
            )

            if not content:
                raise ValueError(
                    f"Decoded content is empty for {attachment.filename}"
                )

            attachment.content = content

            db.commit()

            stats["emails"] += 1
            stats["attachments_stored"] += 1

            logger.info(
                "Attachment retry successful: %s (%d bytes) "
                "for email %s",
                attachment.filename,
                len(content),
                attachment.email_id,
            )

        except Exception:
            db.rollback()

            stats["failures"] += 1

            logger.exception(
                "Attachment retry failed: %s for email %s",
                attachment.filename,
                attachment.email_id,
            )

    logger.info(
        "Attachment retry complete: %d stored, %d failures",
        stats["attachments_stored"],
        stats["failures"],
    )

    return stats