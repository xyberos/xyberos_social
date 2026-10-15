"""Add attachment metadata for objects stored outside PostgreSQL.

Revision ID: 0011_attachments
Revises: 0010_bridge_infrastructure
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_attachments"
down_revision = "0010_bridge_infrastructure"
branch_labels = None
depends_on = None

# Frozen copy of app.core.scoping.SCOPE_TYPES: a migration states the schema it applied.
_SCOPE_CHECK = "scope_type IN ('platform', 'account', 'community', 'conversation', 'business')"
_SCAN_STATE_CHECK = "scan_state IN ('pending_upload', 'pending_scan', 'scanned', 'rejected')"


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("object_key", sa.String(length=255), nullable=False),
        sa.Column("original_filename", sa.String(length=200), nullable=False),
        sa.Column("declared_mime_type", sa.String(length=120), nullable=False),
        sa.Column("detected_mime_type", sa.String(length=120), nullable=True),
        sa.Column("declared_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("scan_state", sa.String(length=16), nullable=False),
        sa.Column("scan_detail", sa.String(length=240), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_SCOPE_CHECK, name="ck_attachment_scope_type"),
        sa.CheckConstraint(_SCAN_STATE_CHECK, name="ck_attachment_scan_state"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_attachment_size"),
        sa.CheckConstraint("length(original_filename) <= 200", name="ck_attachment_filename_length"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["message_id"], ["conversation_messages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key", name="uq_attachment_object_key"),
    )
    op.create_index(
        "ix_attachments_owner_state", "attachments", ["owner_user_id", "scan_state"]
    )
    op.create_index("ix_attachments_message", "attachments", ["message_id"])
    op.create_index(
        "ix_attachments_scope_created", "attachments", ["scope_type", "scope_id", "created_at"]
    )
    op.create_index("ix_attachments_expiry", "attachments", ["scan_state", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_attachments_expiry", table_name="attachments")
    op.drop_index("ix_attachments_scope_created", table_name="attachments")
    op.drop_index("ix_attachments_message", table_name="attachments")
    op.drop_index("ix_attachments_owner_state", table_name="attachments")
    op.drop_table("attachments")
