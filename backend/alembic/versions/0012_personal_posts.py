"""Let a post belong to an account instead of a community.

A personal update has no community and may have no title: the audience is the author's
followers rather than a membership list, so the shape of the row is what distinguishes it.
The `ck_post_shape` constraint states both shapes in the schema instead of in the routes.

Revision ID: 0012_personal_posts
Revises: 0011_attachments
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_personal_posts"
down_revision = "0011_attachments"
branch_labels = None
depends_on = None

# Frozen copy of the constraint as written in this revision: a migration states the schema
# it applied, so it must not follow later edits to the application's own model.
_SHAPE_CHECK = (
    "(community_id IS NOT NULL AND title IS NOT NULL AND length(title) BETWEEN 1 AND 140)"
    " OR (community_id IS NULL AND length(body) BETWEEN 1 AND 10000)"
)


def upgrade() -> None:
    op.drop_constraint("ck_post_title_length", "posts", type_="check")
    op.alter_column("posts", "community_id", existing_type=sa.Uuid(), nullable=True)
    op.alter_column("posts", "title", existing_type=sa.String(length=140), nullable=True)
    op.create_check_constraint("ck_post_shape", "posts", _SHAPE_CHECK)
    op.create_check_constraint(
        "ck_post_title_length", "posts", "title IS NULL OR length(title) <= 140"
    )


def downgrade() -> None:
    # Fails while personal updates exist: there is no community and no title to restore.
    op.drop_constraint("ck_post_title_length", "posts", type_="check")
    op.drop_constraint("ck_post_shape", "posts", type_="check")
    op.create_check_constraint(
        "ck_post_title_length", "posts", "length(title) BETWEEN 1 AND 140"
    )
    op.alter_column("posts", "title", existing_type=sa.String(length=140), nullable=False)
    op.alter_column("posts", "community_id", existing_type=sa.Uuid(), nullable=False)
