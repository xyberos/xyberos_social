"""Let a reader like or dislike a post.

The pair `(post_id, user_id)` is the primary key, so one reader holds at most one verdict on a
post: a second like takes the first back and a dislike replaces it. Counts are read from these
rows, never stored on the post.

Revision ID: 0025_post_reactions
Revises: 0024_moderation_appeals
"""

from alembic import op
import sqlalchemy as sa

revision = "0025_post_reactions"
down_revision = "0024_moderation_appeals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "post_reactions",
        sa.Column(
            "post_id",
            sa.Uuid(),
            sa.ForeignKey("posts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('like', 'dislike')", name="ck_reaction_kind"),
    )
    op.create_index("ix_reactions_post_kind", "post_reactions", ["post_id", "kind"])


def downgrade() -> None:
    op.drop_index("ix_reactions_post_kind", table_name="post_reactions")
    op.drop_table("post_reactions")
