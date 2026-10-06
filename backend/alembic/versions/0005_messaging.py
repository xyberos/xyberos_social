"""Add direct and group messaging.

Revision ID: 0005_messaging
Revises: 0004_community_content
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_messaging"
down_revision = "0004_community_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=6), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=True),
        sa.Column("direct_key", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('direct', 'group')", name="ck_conversation_kind"),
        sa.CheckConstraint(
            "(kind = 'direct' AND direct_key IS NOT NULL AND title IS NULL) OR "
            "(kind = 'group' AND direct_key IS NULL AND title IS NOT NULL)",
            name="ck_conversation_shape",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("direct_key"),
    )
    op.create_index("ix_conversations_created", "conversations", ["created_at"])

    op.create_table(
        "conversation_members",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=6), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('owner', 'member')", name="ck_conversation_member_role"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("conversation_id", "user_id"),
    )
    op.create_index(
        "ix_conversation_members_user", "conversation_members", ["user_id", "conversation_id"]
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sender_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(trim(body)) BETWEEN 1 AND 5000", name="ck_message_body_length"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversation_messages_order",
        "conversation_messages",
        ["conversation_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_messages_order", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversation_members_user", table_name="conversation_members")
    op.drop_table("conversation_members")
    op.drop_index("ix_conversations_created", table_name="conversations")
    op.drop_table("conversations")