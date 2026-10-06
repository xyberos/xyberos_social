"""Add relationships and community participation.

Revision ID: 0003_relationships_communities
Revises: 0002_disable_email_verification
Create Date: 2026-10-06
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_relationships_communities"
down_revision = "0002_disable_email_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_relationships",
        sa.Column("source_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("source_user_id != target_user_id", name="ck_relationship_not_self"),
        sa.CheckConstraint("kind IN ('follow', 'block')", name="ck_relationship_kind"),
        sa.ForeignKeyConstraint(["source_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_user_id", "target_user_id", "kind"),
    )
    op.create_index(
        "ix_relationship_target_kind", "user_relationships", ["target_user_id", "kind"]
    )

    op.create_table(
        "communities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("slug", sa.String(length=48), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("visibility", sa.String(length=8), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("visibility IN ('public', 'private')", name="ck_community_visibility"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_communities_slug", "communities", ["slug"], unique=True)

    op.create_table(
        "community_memberships",
        sa.Column("community_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'moderator', 'member')", name="ck_membership_role"),
        sa.ForeignKeyConstraint(["community_id"], ["communities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("community_id", "user_id"),
    )
    op.create_index(
        "ix_membership_user_community", "community_memberships", ["user_id", "community_id"]
    )

    op.create_table(
        "community_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("community_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("target_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["community_id"], ["communities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_community_audit_created",
        "community_audit_events",
        ["community_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_community_audit_created", table_name="community_audit_events")
    op.drop_table("community_audit_events")
    op.drop_index("ix_membership_user_community", table_name="community_memberships")
    op.drop_table("community_memberships")
    op.drop_index("ix_communities_slug", table_name="communities")
    op.drop_table("communities")
    op.drop_index("ix_relationship_target_kind", table_name="user_relationships")
    op.drop_table("user_relationships")