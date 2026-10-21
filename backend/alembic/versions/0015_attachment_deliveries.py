"""Track who has actually received an attachment's bytes.

The stored object is single-copy, so it is released once every other participant has
downloaded it. One row per attachment and participant records that delivery; the attachment
row stays behind as a tombstone so the transcript can still name the file.

Revision ID: 0015_attachment_deliveries
Revises: 0014_attachment_only_messages
Create Date: 2026-10-21
"""

from alembic import op
import sqlalchemy as sa

revision = "0015_attachment_deliveries"
down_revision = "0014_attachment_only_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attachment_deliveries",
        sa.Column("attachment_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["attachment_id"], ["attachments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("attachment_id", "user_id"),
    )
    op.create_index(
        "ix_attachment_deliveries_user", "attachment_deliveries", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_attachment_deliveries_user", table_name="attachment_deliveries")
    op.drop_table("attachment_deliveries")
