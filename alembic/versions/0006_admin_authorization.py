"""add managed MCP clients, bearer tokens, and permissions

Revision ID: 0006_admin_authorization
Revises: 0005_pubsub_events
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_admin_authorization"
down_revision = "0005_pubsub_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_clients",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_table(
        "admin_tokens",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("admin_clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("token_prefix", sa.String(24), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("ix_admin_tokens_token_hash", "admin_tokens", ["token_hash"])
    op.create_index("ix_admin_tokens_client_id", "admin_tokens", ["client_id"])
    op.create_table(
        "admin_client_permissions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("admin_clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("permission", sa.String(64), nullable=False),
        sa.UniqueConstraint("client_id", "permission", name="uq_admin_client_permission"),
    )
    op.create_index("ix_admin_client_permissions_client_id", "admin_client_permissions", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_admin_client_permissions_client_id", table_name="admin_client_permissions")
    op.drop_table("admin_client_permissions")
    op.drop_index("ix_admin_tokens_client_id", table_name="admin_tokens")
    op.drop_index("ix_admin_tokens_token_hash", table_name="admin_tokens")
    op.drop_table("admin_tokens")
    op.drop_table("admin_clients")