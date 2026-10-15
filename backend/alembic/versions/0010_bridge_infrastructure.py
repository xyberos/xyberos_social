"""Add the platform audit log, the transactional outbox, and idempotency keys.

Revision ID: 0010_bridge_infrastructure
Revises: 0009_user_settings
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_bridge_infrastructure"
down_revision = "0009_user_settings"
branch_labels = None
depends_on = None

# Frozen copy of app.core.scoping.SCOPE_TYPES: a migration states the schema it applied,
# so it must not follow later edits to the application's own constants.
_SCOPE_CHECK = "scope_type IN ('platform', 'account', 'community', 'conversation', 'business')"


def upgrade() -> None:
    op.create_table(
        "platform_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("before_state", sa.JSON(), nullable=True),
        sa.Column("after_state", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(_SCOPE_CHECK, name="ck_platform_audit_scope_type"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_platform_audit_scope_created",
        "platform_audit_events",
        ["scope_type", "scope_id", "created_at"],
    )
    op.create_index(
        "ix_platform_audit_actor_created",
        "platform_audit_events",
        ["actor_user_id", "created_at"],
    )
    op.create_index(
        "ix_platform_audit_action_created",
        "platform_audit_events",
        ["action", "created_at"],
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=True),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_SCOPE_CHECK, name="ck_outbox_scope_type"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_unpublished", "outbox_events", ["published_at", "created_at"])
    op.create_index("ix_outbox_aggregate", "outbox_events", ["aggregate_type", "aggregate_id"])

    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=80), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("endpoint", sa.String(length=120), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=12), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_location", sa.String(length=320), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('in_progress', 'completed')", name="ck_idempotency_state"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
    )
    op.create_index("ix_idempotency_created", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_created", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")

    op.drop_index("ix_outbox_aggregate", table_name="outbox_events")
    op.drop_index("ix_outbox_unpublished", table_name="outbox_events")
    op.drop_table("outbox_events")

    op.drop_index("ix_platform_audit_action_created", table_name="platform_audit_events")
    op.drop_index("ix_platform_audit_actor_created", table_name="platform_audit_events")
    op.drop_index("ix_platform_audit_scope_created", table_name="platform_audit_events")
    op.drop_table("platform_audit_events")
