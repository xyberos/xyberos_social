"""Index author-scoped finite update retrieval.

Revision ID: 0007_updates_author_index
Revises: 0006_group_membership_events
Create Date: 2026-10-06
"""

from alembic import op

revision = "0007_updates_author_index"
down_revision = "0006_group_membership_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_posts_author_created", "posts", ["author_id", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_posts_author_created", table_name="posts")