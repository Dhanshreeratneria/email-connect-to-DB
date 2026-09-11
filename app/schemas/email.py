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


class EmailDeliveryOut(BaseModel):
    account_id: int
    gmail_message_id: str
    delivery_type: str
    received_at: datetime

    class Config:
        from_attributes = True
