"""Store the object-storage reference for an account avatar.

Revision ID: 0021_user_avatar
Revises: 0020_user_appearance
"""

from alembic import op
import sqlalchemy as sa


revision = "0021_user_avatar"
down_revision = "0020_user_appearance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_key", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("avatar_mime_type", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_mime_type")
    op.drop_column("users", "avatar_key")
