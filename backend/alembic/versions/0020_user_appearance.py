"""Store curated appearance preferences for each account.

Revision ID: 0020_user_appearance
Revises: 0019_media_derivatives
"""

from alembic import op
import sqlalchemy as sa


revision = "0020_user_appearance"
down_revision = "0019_media_derivatives"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("appearance_preset", sa.String(length=16), server_default="forest", nullable=False)
    )
    op.add_column(
        "users", sa.Column("background_style", sa.String(length=16), server_default="paper", nullable=False)
    )
    op.add_column(
        "users", sa.Column("display_density", sa.String(length=16), server_default="comfortable", nullable=False)
    )
    op.create_check_constraint(
        "ck_user_appearance_preset", "users", "appearance_preset IN ('forest', 'ocean', 'violet', 'sunset', 'midnight')"
    )
    op.create_check_constraint(
        "ck_user_background_style", "users", "background_style IN ('paper', 'mist', 'dawn', 'aurora')"
    )
    op.create_check_constraint(
        "ck_user_display_density", "users", "display_density IN ('comfortable', 'compact')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_user_display_density", "users", type_="check")
    op.drop_constraint("ck_user_background_style", "users", type_="check")
    op.drop_constraint("ck_user_appearance_preset", "users", type_="check")
    op.drop_column("users", "display_density")
    op.drop_column("users", "background_style")
    op.drop_column("users", "appearance_preset")
