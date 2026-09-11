"""add category, rfc_message_id, and email_deliveries (multi-inbox To/Cc/Bcc dedupe)

Revision ID: 0002_categories_and_deliveries
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_categories_and_deliveries"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    # 1. New columns on emails
    op.add_column("emails", sa.Column("category", sa.String(32), nullable=True))
    op.add_column("emails", sa.Column("rfc_message_id", sa.String(998), nullable=True))
    op.create_index("ix_emails_rfc_message_id", "emails", ["rfc_message_id"])
    op.create_index("ix_emails_category", "emails", ["category"])

    # 2. New junction table: which account(s) received each email, and how
    op.create_table(
        "email_deliveries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "email_id",
            sa.Integer,
            sa.ForeignKey("emails.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Integer,
            sa.ForeignKey("gmail_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("gmail_message_id", sa.String(255), nullable=False),
        sa.Column("delivery_type", sa.String(16), nullable=False, server_default="to"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "account_id", "gmail_message_id", name="uq_account_gmail_message"
        ),
    )
    op.create_index("ix_email_deliveries_email_id", "email_deliveries", ["email_id"])
    op.create_index("ix_email_deliveries_account_id", "email_deliveries", ["account_id"])

    # 3. Backfill: every existing email row becomes one delivery row.
    #    All existing rows predate this feature, so they're recorded as "to"
    #    (we have no header data on disk to know otherwise for old rows).
    op.execute(
        """
        INSERT INTO email_deliveries (email_id, account_id, gmail_message_id, delivery_type, received_at)
        SELECT id, account_id, message_id, 'to', received_at FROM emails
        """
    )

    # 4. Remove the now-obsolete per-account uniqueness and columns from
    #    emails. Names below match the ACTUAL production schema (confirmed
    #    via \d emails), not the original migration file's guessed names.
    op.drop_constraint("uq_emails_account_message", "emails", type_="unique")
    op.drop_constraint("emails_account_id_fkey", "emails", type_="foreignkey")
    op.drop_index("ix_emails_account_id", table_name="emails")
    op.drop_index("ix_emails_account_received_at", table_name="emails")
    op.drop_index("ix_emails_message_id", table_name="emails")
    op.drop_column("emails", "account_id")
    op.drop_column("emails", "message_id")


def downgrade():
    op.add_column("emails", sa.Column("message_id", sa.String(255), nullable=True))
    op.add_column("emails", sa.Column("account_id", sa.Integer, nullable=True))

    # Best-effort: only correct if each email had exactly one delivery.
    op.execute(
        """
        UPDATE emails e
        SET account_id = d.account_id, message_id = d.gmail_message_id
        FROM email_deliveries d
        WHERE d.email_id = e.id
        """
    )

    op.alter_column("emails", "account_id", nullable=False)
    op.alter_column("emails", "message_id", nullable=False)
    op.create_index("ix_emails_account_id", "emails", ["account_id"])
    op.create_index(
        "ix_emails_account_received_at", "emails", ["account_id", "received_at"]
    )
    op.create_index("ix_emails_message_id", "emails", ["message_id"])
    op.create_foreign_key(
        "emails_account_id_fkey",
        "emails",
        "gmail_accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_emails_account_message", "emails", ["account_id", "message_id"]
    )

    op.drop_table("email_deliveries")
    op.drop_index("ix_emails_category", table_name="emails")
    op.drop_index("ix_emails_rfc_message_id", table_name="emails")
    op.drop_column("emails", "rfc_message_id")
    op.drop_column("emails", "category")
