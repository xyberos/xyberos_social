"""Persist call records, per-participant outcomes, and call settings.

A call row holds metadata only: conversation, initiator, media, state, and the times that
describe the ring. Media is peer-to-peer and has no table, path, or column here. Participant
rows record who was invited, who joined, and how each of them left, which is what makes a
missed-call notification possible without storing any content.

Revision ID: 0016_calls
Revises: 0015_attachment_deliveries
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa

revision = "0016_calls"
down_revision = "0015_attachment_deliveries"
branch_labels = None
depends_on = None

_CALL_STATES = "state IN ('ringing', 'accepted', 'ended')"
_CALL_MEDIA = "media IN ('audio', 'video')"
_CALL_END_REASONS = (
    "end_reason IS NULL OR end_reason IN "
    "('hangup', 'declined', 'missed', 'busy', 'failed', 'cancelled')"
)
_PARTICIPANT_STATES = "state IN ('invited', 'joined', 'declined', 'left', 'missed')"
_NOTIFICATION_CATEGORIES = "category IN ('reply', 'moderation', 'call')"
_OLD_NOTIFICATION_CATEGORIES = "category IN ('reply', 'moderation')"


def upgrade() -> None:
    op.create_table(
        "calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("initiator_user_id", sa.Uuid(), nullable=False),
        sa.Column("media", sa.String(length=8), nullable=False),
        sa.Column("state", sa.String(length=8), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=16), nullable=True),
        sa.CheckConstraint(_CALL_MEDIA, name="ck_call_media"),
        sa.CheckConstraint(_CALL_STATES, name="ck_call_state"),
        sa.CheckConstraint(_CALL_END_REASONS, name="ck_call_end_reason"),
        sa.CheckConstraint(
            "(state = 'ended' AND ended_at IS NOT NULL) OR (state != 'ended')",
            name="ck_call_ended_at",
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["initiator_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_calls_conversation_started", "calls", ["conversation_id", "started_at"])
    op.create_index("ix_calls_state_started", "calls", ["state", "started_at"])

    op.create_table(
        "call_participants",
        sa.Column("call_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=8), nullable=False),
        sa.Column(
            "invited_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_PARTICIPANT_STATES, name="ck_call_participant_state"),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("call_id", "user_id"),
    )
    op.create_index("ix_call_participants_user", "call_participants", ["user_id", "call_id"])

    op.add_column(
        "users",
        sa.Column("notify_calls", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("accept_calls", sa.Boolean(), server_default=sa.true(), nullable=False),
    )

    op.drop_constraint("ck_notification_category", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notification_category", "notifications", _NOTIFICATION_CATEGORIES
    )


def downgrade() -> None:
    op.drop_constraint("ck_notification_category", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notification_category", "notifications", _OLD_NOTIFICATION_CATEGORIES
    )
    op.drop_column("users", "accept_calls")
    op.drop_column("users", "notify_calls")
    op.drop_index("ix_call_participants_user", table_name="call_participants")
    op.drop_table("call_participants")
    op.drop_index("ix_calls_state_started", table_name="calls")
    op.drop_index("ix_calls_conversation_started", table_name="calls")
    op.drop_table("calls")
