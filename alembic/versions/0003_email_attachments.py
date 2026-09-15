"""add email_attachments table (stores attachment bytes: pdf/zip/image/etc.)

Revision ID: 0003_email_attachments
Revises: 0002_categories_and_deliveries
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_email_attachments"
down_revision = "0002_categories_and_deliveries"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "email_attachments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "email_id",
            sa.Integer,
            sa.ForeignKey("emails.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("gmail_attachment_id", sa.String(512), nullable=False),
        sa.Column("filename", sa.String(1024), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=True),
        sa.Column("size", sa.BigInteger, nullable=True),
        sa.Column("content", sa.LargeBinary, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "email_id", "gmail_attachment_id", name="uq_email_gmail_attachment"
        ),
    )
    op.create_index(
        "ix_email_attachments_email_id", "email_attachments", ["email_id"]
    )


def downgrade():
    op.drop_index("ix_email_attachments_email_id", table_name="email_attachments")
    op.drop_table("email_attachments")
