"""make rfc message id unique

Revision ID: 0004_unique_rfc_message_id
Revises: 0003_email_attachments
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_unique_rfc_message_id"
down_revision = "0003_email_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_emails_rfc_message_id",
        "emails",
        ["rfc_message_id"],
        unique=True,
        postgresql_where=sa.text(
            "rfc_message_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_emails_rfc_message_id",
        table_name="emails",
    )