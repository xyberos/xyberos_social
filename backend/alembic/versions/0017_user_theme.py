"""Give every account a theme preference.

The value is one of `system`, `light`, or `dark`; `system` is the default so an existing
account keeps following its device until it chooses. Signed-out pages read the same choice
from a cookie, which the theme endpoint sets alongside this column.

Revision ID: 0017_user_theme
Revises: 0016_calls
Create Date: 2026-10-22
"""

from alembic import op
import sqlalchemy as sa

revision = "0017_user_theme"
down_revision = "0016_calls"
branch_labels = None
depends_on = None

_THEME_CHOICES = "theme IN ('system', 'light', 'dark')"


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("theme", sa.String(length=8), server_default="system", nullable=False),
    )
    op.create_check_constraint("ck_user_theme", "users", _THEME_CHOICES)


def downgrade() -> None:
    op.drop_constraint("ck_user_theme", "users", type_="check")
    op.drop_column("users", "theme")
