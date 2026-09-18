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


def decode_base64_urlsafe_bytes(value: str) -> bytes:
    """
    Same URL-safe Base64 padding fix as decode_base64_urlsafe(), but
    returns raw bytes instead of decoding as text. Use this for binary
    attachment content (PDF/zip/image/etc.) — text decoding would
    corrupt non-text bytes.
    """
    if not value:
        return b""

    padded_value = value + "=" * (-len(value) % 4)

    return base64.urlsafe_b64decode(padded_value.encode("utf-8"))


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


def build_recipients(headers: dict[str, str]) -> list[dict[str, str | None]]:
    """
    Parses the To/Cc/Bcc headers into a structured recipient list, each
    entry tagged with its "type" ("to" | "cc" | "bcc") plus the
    recipient's display name and lowercased email address.

    This replaces the old behaviour of naively splitting the header
    strings on "," into one flat, untyped list — which both lost the
    to/cc/bcc distinction and mishandled display names containing a
    comma (e.g. 'Doe, Jane <jane@example.com>'), since getaddresses()
    parses each header value as a proper RFC 2822 address list instead.

    Note: Gmail never includes the Bcc header on a message as delivered
    to any of its recipients — it's only present when reading a
    message directly from the sender's own Sent items. So a "bcc"
    entry will only ever show up there, not on a received copy.
    """
    recipients: list[dict[str, str | None]] = []

    for header_name in ("to", "cc", "bcc"):
        header_value = headers.get(header_name, "")

        if not header_value:
            continue

        for name, addr in getaddresses([header_value]):
            addr = (addr or "").strip().lower()

            if not addr:
                continue

            recipients.append(
                {
                    "name": name.strip() or None,
                    "email": addr,
                    "type": header_name,
                }
            )

    return recipients


# Maps a MIME subtype (the part after "/") to a short, human-friendly
# attachment type. Subtypes not listed here fall back to the MIME
# top-level type (image/video/audio/text) or "other".
ATTACHMENT_MIME_SUBTYPE_MAP = {
    "pdf": "pdf",
    "zip": "archive",
    "x-zip-compressed": "archive",
    "x-rar-compressed": "archive",
    "x-7z-compressed": "archive",
    "gzip": "archive",
    "x-gzip": "archive",
    "x-tar": "archive",
    "msword": "document",
    "vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "vnd.oasis.opendocument.text": "document",
    "rtf": "document",
    "vnd.ms-excel": "spreadsheet",
    "vnd.openxmlformats-officedocument.spreadsheetml.sheet": "spreadsheet",
    "vnd.oasis.opendocument.spreadsheet": "spreadsheet",
    "csv": "spreadsheet",
    "vnd.ms-powerpoint": "presentation",
    "vnd.openxmlformats-officedocument.presentationml.presentation": "presentation",
    "vnd.oasis.opendocument.presentation": "presentation",
    "json": "data",
    "xml": "data",
}


def classify_attachment_type(mime_type: str | None) -> str:
    """
    Derives a short, human-friendly attachment type ("image", "pdf",
    "document", "spreadsheet", "presentation", "archive", "video",
    "audio", "text", "data", or "other") from a raw MIME type, so
    callers don't have to pattern-match "application/vnd.openxml..."
    strings themselves.
    """
    if not mime_type or "/" not in mime_type:
        return "other"

    primary, _, subtype = mime_type.lower().partition("/")
    subtype = subtype.split(";", 1)[0].strip()

    if primary in ("image", "video", "audio"):
        return primary

    if primary == "text":
        return ATTACHMENT_MIME_SUBTYPE_MAP.get(subtype, "text")

    return ATTACHMENT_MIME_SUBTYPE_MAP.get(subtype, "other")


def parse_message(message: dict[str, Any]) -> dict[str, Any]:
    """
    Converts Gmail API messages.get(format='full') payload into
    PostgreSQL-ready email fields.

    This stores:
    - Gmail thread ID
    - RFC Message-ID header (stable across every recipient's copy
      of the same email, used to dedupe across connected accounts)
    - Sender
    - To/CC/BCC recipients, each tagged with its type (display list)
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

    recipients = build_recipients(headers)

    plain_text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []

    def walk_part(part: dict[str, Any]) -> None:
        mime_type = part.get("mime_type", "")
        body = part.get("body") or {}
        filename = part.get("filename") or ""

        attachment_id = body.get("attachmentId")

        if filename and attachment_id:
            attachments.append(
                {
                    "filename": filename,
                    "mime_type": mime_type,
                    "type": classify_attachment_type(mime_type),
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
        # Extra, non-column fields for delivery-type detection. Derived
        # from the same `recipients` list above so there's one source
        # of truth for who's To/Cc — not re-parsed separately.
        "to_addresses": [r["email"] for r in recipients if r["type"] == "to"],
        "cc_addresses": [r["email"] for r in recipients if r["type"] == "cc"],
    }