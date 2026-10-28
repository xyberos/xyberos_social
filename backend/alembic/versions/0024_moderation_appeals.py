"""Let an author appeal a platform moderator's removal.

A removal the platform queue decided needs somewhere to be questioned, and the author is the
only account entitled to question it. The table records the request, the decision it answers,
and the outcome the same platform role records — so the appeal is reviewable, not a fresh
judgement with no history behind it.

Revision ID: 0024_moderation_appeals
Revises: 0023_platform_moderation
"""

from alembic import op
import sqlalchemy as sa

revision = "0024_moderation_appeals"
down_revision = "0023_platform_moderation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "moderation_appeals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "author_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "report_id",
            sa.Uuid(),
            sa.ForeignKey("content_reports.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("target_type", sa.String(length=8), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=10), nullable=False, server_default="pending"
        ),
        sa.Column(
            "resolved_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("target_type IN ('post', 'comment')", name="ck_appeal_target_type"),
        sa.CheckConstraint(
            "status IN ('pending', 'granted', 'denied')", name="ck_appeal_status"
        ),
        sa.CheckConstraint(
            "length(reason) BETWEEN 1 AND 2000", name="ck_appeal_reason_length"
        ),
        sa.UniqueConstraint("author_id", "target_type", "target_id", name="uq_appeal_target"),
    )
    op.create_index(
        "ix_appeals_status_created", "moderation_appeals", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_appeals_status_created", table_name="moderation_appeals")
    op.drop_table("moderation_appeals")
