"""Let a personal update be readable on the open web.

`everyone` always meant every account on this instance, never the open web, so publishing to
the web needs a value of its own rather than a change to that one. Existing rows keep the
audience they were written under: nothing becomes public by upgrading.

Revision ID: 0022_public_audience
Revises: 0021_user_avatar
"""

from alembic import op

revision = "0022_public_audience"
down_revision = "0021_user_avatar"
branch_labels = None
depends_on = None

# Frozen copies: a migration states the schema it applied, so it must not follow later edits
# to the application's own model.
_AUDIENCE_CHECK = "audience IN ('only_me', 'followers', 'everyone', 'public')"
_PREVIOUS_CHECK = "audience IN ('only_me', 'followers', 'everyone')"


def upgrade() -> None:
    op.drop_constraint("ck_post_audience", "posts", type_="check")
    op.create_check_constraint("ck_post_audience", "posts", _AUDIENCE_CHECK)


def downgrade() -> None:
    # The older schema has no `public`, so the rows have to move before the constraint can.
    # Narrowing to `followers` is the safe direction: the post stays readable by the people it
    # was already readable by, and stops being readable by the open web.
    op.execute("UPDATE posts SET audience = 'followers' WHERE audience = 'public'")
    op.drop_constraint("ck_post_audience", "posts", type_="check")
    op.create_check_constraint("ck_post_audience", "posts", _PREVIOUS_CHECK)
