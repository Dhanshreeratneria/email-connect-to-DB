import base64
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any


# Maps Gmail's category label IDs to a clean, storable value.
# Anything without one of these labels is treated as "primary"
# (Gmail's default inbox tab).
CATEGORY_LABEL_MAP = {
    "CATEGORY_PERSONAL": "primary",
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_PROMOTIONS": "promotions",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_FORUMS": "forums",
}


def categorize(label_ids: list[str]) -> str:
    """
    Returns one of: primary, social, promotions, updates, forums.
    """
    for label in label_ids:
        if label in CATEGORY_LABEL_MAP:
            return CATEGORY_LABEL_MAP[label]

    return "primary"


def decode_base64_urlsafe(value: str) -> str:
    """
    Gmail returns body data as URL-safe Base64 without guaranteed padding.
    """
    if not value:
        return ""

    padded_value = value + "=" * (-len(value) % 4)

    return base64.urlsafe_b64decode(
        padded_value.encode("utf-8")
    ).decode("utf-8", errors="replace")


def parse_received_at(message: dict[str, Any], headers: dict[str, str]) -> datetime:
    date_header = headers.get("date")

    if date_header:
        try:
            value = parsedate_to_datetime(date_header)

            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)

            return value.astimezone(timezone.utc)

        except (TypeError, ValueError, IndexError):
            pass

    internal_date = message.get("internalDate")

    if internal_date:
        try:
            return datetime.fromtimestamp(
                int(internal_date) / 1000,
                tz=timezone.utc,
            )
        except (TypeError, ValueError):
            pass

    return datetime.now(timezone.utc)


def extract_addresses(header_value: str) -> list[str]:
    """
    Parses a raw To/Cc header value (which may contain multiple,
    comma-separated "Name <email>" entries) into a clean lowercase
    list of just the email addresses.
    """
    if not header_value:
        return []

    return [
        addr.strip().lower()
        for _, addr in getaddresses([header_value])
        if addr and addr.strip()
    ]


def parse_message(message: dict[str, Any]) -> dict[str, Any]:
    """
    Converts Gmail API messages.get(format='full') payload into
    PostgreSQL-ready email fields.

    This stores:
    - Gmail thread ID
    - RFC Message-ID header (stable across every recipient's copy
      of the same email, used to dedupe across connected accounts)
    - Sender
    - To/CC/BCC recipients (display list)
    - Subject
    - Plain-text MIME body
    - Labels + derived category (primary/social/promotions/updates/forums)
    - Attachment metadata

    Also returns two extra, non-column keys — "to_addresses" and
    "cc_addresses" — which the sync service uses to work out whether
    a given connected account received this email as To, Cc, or Bcc.
    These two keys must be popped before constructing an Email(**parsed).
    """

    payload = message.get("payload") or {}

    headers = {
        item.get("name", "").lower(): item.get("value", "")
        for item in payload.get("headers", [])
        if item.get("name")
    }

    sender_name, sender_email = parseaddr(headers.get("from", ""))

    recipients: list[str] = []

    for header_name in ("to", "cc", "bcc"):
        header_value = headers.get(header_name, "")

        if header_value:
            recipients.extend(
                item.strip()
                for item in header_value.split(",")
                if item.strip()
            )

    plain_text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []

    def walk_part(part: dict[str, Any]) -> None:
        mime_type = part.get("mimeType", "")
        body = part.get("body") or {}
        filename = part.get("filename") or ""

        attachment_id = body.get("attachmentId")

        if filename and attachment_id:
            attachments.append(
                {
                    "filename": filename,
                    "mime_type": mime_type,
                    "attachment_id": attachment_id,
                    "size": body.get("size", 0),
                }
            )

        encoded_data = body.get("data")

        if encoded_data:
            decoded = decode_base64_urlsafe(encoded_data)

            if mime_type == "text/plain":
                plain_text_parts.append(decoded)

            elif mime_type == "text/html":
                html_parts.append(decoded)

        for child_part in part.get("parts") or []:
            walk_part(child_part)

    walk_part(payload)

    body_text = "\n".join(plain_text_parts).strip()

    # Fallback: retain HTML if no text/plain MIME body exists.
    if not body_text and html_parts:
        body_text = "\n".join(html_parts).strip()

    label_ids = message.get("labelIds") or []

    return {
        "rfc_message_id": headers.get("message-id") or None,
        "thread_id": message.get("threadId", ""),
        "sender_name": sender_name or None,
        "sender_email": sender_email or headers.get("from", ""),
        "recipients": recipients,
        "subject": headers.get("subject", ""),
        "body_text": body_text,
        "received_at": parse_received_at(message, headers),
        "labels": label_ids,
        "category": categorize(label_ids),
        "has_attachments": len(attachments) > 0,
        "attachments": attachments,
        # Extra, non-column fields for delivery-type detection:
        "to_addresses": extract_addresses(headers.get("to", "")),
        "cc_addresses": extract_addresses(headers.get("cc", "")),
    }
