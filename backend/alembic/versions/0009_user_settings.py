"""Store user update-window defaults.

Revision ID: 0009_user_settings
Revises: 0008_notifications
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_user_settings"
down_revision = "0008_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("updates_source", sa.String(length=16), server_default="all", nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("updates_days", sa.Integer(), server_default="7", nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("updates_limit", sa.Integer(), server_default="20", nullable=False),
    )
    op.create_check_constraint(
        "ck_user_updates_source", "users", "updates_source IN ('all', 'people', 'communities')"
    )
    op.create_check_constraint("ck_user_updates_days", "users", "updates_days IN (1, 7, 30, 90)")
    op.create_check_constraint(
        "ck_user_updates_limit", "users", "updates_limit IN (1, 5, 10, 20, 50)"
    )


def downgrade() -> None:
    op.drop_constraint("ck_user_updates_limit", "users", type_="check")
    op.drop_constraint("ck_user_updates_days", "users", type_="check")
    op.drop_constraint("ck_user_updates_source", "users", type_="check")
    op.drop_column("users", "updates_limit")
    op.drop_column("users", "updates_days")
    op.drop_column("users", "updates_source")