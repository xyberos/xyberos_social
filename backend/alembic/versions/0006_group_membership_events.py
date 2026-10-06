"""Track group departures and membership audit events.

Revision ID: 0006_group_membership_events
Revises: 0005_messaging
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_group_membership_events"
down_revision = "0005_messaging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_members",
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "conversation_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("subject_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "action IN ('member_added', 'member_removed', 'member_left')",
            name="ck_conversation_event_action",
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversation_events_order", "conversation_events", ["conversation_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_events_order", table_name="conversation_events")
    op.drop_table("conversation_events")
    op.drop_column("conversation_members", "left_at")