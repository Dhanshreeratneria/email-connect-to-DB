from datetime import datetime

from pydantic import BaseModel


class EmailOut(BaseModel):
    id: int
    rfc_message_id: str | None
    thread_id: str
    sender_name: str | None
    sender_email: str
    recipients: list
    subject: str | None
    body_text: str | None
    received_at: datetime
    labels: list
    category: str | None
    has_attachments: bool
    attachments: list

    class Config:
        from_attributes = True


class AttachmentOut(BaseModel):
    """
    Attachment metadata only — never the binary `content` column, so
    listing an email's attachments stays small even when the file
    itself (PDF/zip/image/etc.) is large. Fetch the actual bytes via
    the separate /attachments/{attachment_id}/download endpoint.
    """

    id: int
    gmail_attachment_id: str
    filename: str
    mime_type: str | None
    size: int | None

    class Config:
        from_attributes = True


class EmailDeliveryOut(BaseModel):
    account_id: int
    gmail_message_id: str
    delivery_type: str
    received_at: datetime

    class Config:
        from_attributes = True
