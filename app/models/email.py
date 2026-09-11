from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class GmailAccount(Base):
    __tablename__ = "gmail_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    google_email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    encrypted_token: Mapped[str] = mapped_column(Text)
    history_id: Mapped[str | None] = mapped_column(String(64))
    watch_expiration: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    deliveries: Mapped[list["EmailDelivery"]] = relationship(back_populates="account")


class Email(Base):
    """
    One row per unique real-world email (keyed by its RFC Message-ID
    header), regardless of how many connected inboxes received it.
    Which account(s) received it, and how (To/Cc/Bcc), lives in
    EmailDelivery.
    """

    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rfc_message_id: Mapped[str | None] = mapped_column(String(998), index=True)
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    sender_name: Mapped[str | None] = mapped_column(String(500))
    sender_email: Mapped[str] = mapped_column(String(320), index=True)
    recipients: Mapped[list] = mapped_column(JSON)
    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    labels: Mapped[list] = mapped_column(JSON)
    category: Mapped[str | None] = mapped_column(String(32), index=True)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    attachments: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    deliveries: Mapped[list["EmailDelivery"]] = relationship(
        back_populates="email", cascade="all, delete-orphan"
    )


class EmailDelivery(Base):
    """
    Tracks which connected inbox(es) received a given email, and how
    (To / Cc / Bcc). Prevents storing the same email content twice when
    it lands in more than one of your connected Gmail accounts.
    """

    __tablename__ = "email_deliveries"
    __table_args__ = (
        UniqueConstraint("account_id", "gmail_message_id", name="uq_account_gmail_message"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="CASCADE"), index=True
    )
    gmail_message_id: Mapped[str] = mapped_column(String(255))
    delivery_type: Mapped[str] = mapped_column(String(16), default="to")  # "to" | "cc" | "bcc"
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    email: Mapped["Email"] = relationship(back_populates="deliveries")
    account: Mapped["GmailAccount"] = relationship(back_populates="deliveries")
