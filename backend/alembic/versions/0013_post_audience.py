"""Give a personal update an audience instead of an implicit one.

Until now a personal update was readable by its author and by the author's followers, because
that is who the read rule allowed. The audience becomes a column so the author can choose
`only_me`, `followers`, or `everyone`; existing rows keep the semantics they were written
under, and a community post keeps the default because its audience is its community.

Revision ID: 0013_post_audience
Revises: 0012_personal_posts
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0013_post_audience"
down_revision = "0012_personal_posts"
branch_labels = None
depends_on = None

# Frozen copies: a migration states the schema it applied, so it must not follow later edits
# to the application's own model.
_AUDIENCE_CHECK = "audience IN ('only_me', 'followers', 'everyone')"
_SCOPE_CHECK = "community_id IS NULL OR audience = 'followers'"


def upgrade() -> None:
    op.add_column(
        "posts",
        sa.Column("audience", sa.String(length=16), nullable=False, server_default="followers"),
    )
    op.create_check_constraint("ck_post_audience", "posts", _AUDIENCE_CHECK)
    op.create_check_constraint("ck_post_audience_scope", "posts", _SCOPE_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_post_audience_scope", "posts", type_="check")
    op.drop_constraint("ck_post_audience", "posts", type_="check")
    op.drop_column("posts", "audience")
