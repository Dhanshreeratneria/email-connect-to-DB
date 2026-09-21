from datetime import datetime

from pydantic import BaseModel, model_validator

from app.config import settings
from app.services.attachment_service import is_displayable_in_claude


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
    # BUG FIX: these two were missing, so every attachment listed via
    # GET /emails/{message_id}/attachments came back with no way to know
    # whether Claude/VS Code could preview it or where to download it from
    # — callers had to hand-build the URL themselves from `id`.
    is_displayable: bool = False
    download_url: str = ""
    view_url: str = ""

    class Config:
        from_attributes = True

    @model_validator(mode="after")
    def _add_computed_fields(self) -> "AttachmentOut":
        self.is_displayable = is_displayable_in_claude(self.mime_type)
        base = settings.public_base_url.rstrip("/")
        self.download_url = f"{base}/attachments/{self.id}/download"
        self.view_url = f"{base}/attachments/{self.id}/view"
        return self


class EmailDeliveryOut(BaseModel):
    account_id: int
    gmail_message_id: str
    delivery_type: str
    received_at: datetime

    class Config:
        from_attributes = True