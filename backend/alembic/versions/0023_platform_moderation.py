"""Give the platform a moderator, and let a report name no community.

A personal update belongs to an account, not a community, so a report about one had nowhere
to go: `content_reports.community_id` was mandatory and every queue required a community
role. The column becomes nullable to mean "platform queue", and `users.is_moderator` is the
role that opens it.

Revision ID: 0023_platform_moderation
Revises: 0022_public_audience
"""

from alembic import op
import sqlalchemy as sa

revision = "0023_platform_moderation"
down_revision = "0022_public_audience"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The default exists to fill the rows that are already there; the application always
    # states the value, so it is dropped again to leave one source of truth.
    op.add_column(
        "users",
        sa.Column("is_moderator", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column(
        "users", "is_moderator", existing_type=sa.Boolean(), server_default=None
    )
    op.alter_column(
        "content_reports", "community_id", existing_type=sa.Uuid(), nullable=True
    )


def downgrade() -> None:
    # A platform report has no community to belong to in the older schema, and keeping the
    # rows would mean inventing one. The audit log keeps the recorded decisions.
    op.execute("DELETE FROM content_reports WHERE community_id IS NULL")
    op.alter_column(
        "content_reports", "community_id", existing_type=sa.Uuid(), nullable=False
    )
    op.drop_column("users", "is_moderator")
