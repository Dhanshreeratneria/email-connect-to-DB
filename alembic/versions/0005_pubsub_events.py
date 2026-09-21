"""add durable Pub/Sub message idempotency table

Revision ID: 0005_pubsub_events
Revises: 0004_unique_rfc_message_id
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_pubsub_events"
down_revision = "0004_unique_rfc_message_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pubsub_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("pubsub_message_id", sa.String(512), nullable=False, unique=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_pubsub_events_pubsub_message_id",
        "pubsub_events",
        ["pubsub_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pubsub_events_pubsub_message_id", table_name="pubsub_events")
    op.drop_table("pubsub_events")
