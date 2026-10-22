"""Let a post carry media.

A post attachment follows the same pipeline as a chat attachment — target, verify, scan, then
bind — but it is owned by the post rather than by a message, and it is not released once a
reader has it: a post is read by whoever arrives later, so its bytes stay until the account
removes them. `post_id` is what the download rule reads to decide who may open the file.

A personal update may now be media alone, so the shape check allows an empty body. The route
still refuses a post that carries neither text nor media.

Revision ID: 0018_post_media
Revises: 0017_user_theme
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa

revision = "0018_post_media"
down_revision = "0017_user_theme"
branch_labels = None
depends_on = None

# Frozen copies: a migration states the schema it applied, so it must not follow later edits
# to the application's own model.
_SHAPE_WITH_TEXT = (
    "(community_id IS NOT NULL AND title IS NOT NULL"
    " AND length(title) BETWEEN 1 AND 140)"
    " OR (community_id IS NULL AND length(body) BETWEEN 1 AND 10000)"
)
_SHAPE_WITH_MEDIA = (
    "(community_id IS NOT NULL AND title IS NOT NULL"
    " AND length(title) BETWEEN 1 AND 140)"
    " OR (community_id IS NULL AND length(body) <= 10000)"
)


def upgrade() -> None:
    op.add_column("attachments", sa.Column("post_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_attachments_post_id",
        "attachments",
        "posts",
        ["post_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_attachments_post", "attachments", ["post_id"])
    op.drop_constraint("ck_post_shape", "posts", type_="check")
    op.create_check_constraint("ck_post_shape", "posts", _SHAPE_WITH_MEDIA)


def downgrade() -> None:
    # Fails if a media-only update exists: narrowing the check would discard it.
    op.drop_constraint("ck_post_shape", "posts", type_="check")
    op.create_check_constraint("ck_post_shape", "posts", _SHAPE_WITH_TEXT)
    op.drop_index("ix_attachments_post", table_name="attachments")
    op.drop_constraint("fk_attachments_post_id", "attachments", type_="foreignkey")
    op.drop_column("attachments", "post_id")
