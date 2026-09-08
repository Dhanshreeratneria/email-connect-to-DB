import base64
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any


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


def parse_message(message: dict[str, Any]) -> dict[str, Any]:
    """
    Converts Gmail API messages.get(format='full') payload into
    PostgreSQL-ready email fields.

    This stores:
    - Gmail message/thread IDs
    - Sender
    - To/CC/BCC recipients
    - Subject
    - Plain-text MIME body
    - Labels
    - Attachment metadata
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

    return {
        "message_id": message["id"],
        "thread_id": message.get("threadId", ""),
        "sender_name": sender_name or None,
        "sender_email": sender_email or headers.get("from", ""),
        "recipients": recipients,
        "subject": headers.get("subject", ""),
        "body_text": body_text,
        "received_at": parse_received_at(message, headers),
        "labels": message.get("labelIds") or [],
        "has_attachments": len(attachments) > 0,
        "attachments": attachments,
    }