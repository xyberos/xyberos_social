"""The append-only platform audit log shared by every domain.

One row per privileged change, whatever the domain: actor, action, target, before/after
state, reason, and request id. The commercial phases extend this table rather than adding
another typed audit table.

Two deliberate differences from the typed `community_audit_events` and `moderation_events`
tables, which stay as the tenant-scoped, cascade-with-their-community domain history:
`scope_id` and `target_id` carry no foreign key, so a row survives the deletion of the
thing it describes, and `actor_user_id` is `SET NULL` so a deleted account does not erase
the record of what it did.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.scoping import scope_type_check_sql


class PlatformAuditEvent(Base):
    __tablename__ = "platform_audit_events"
    __table_args__ = (
        CheckConstraint(scope_type_check_sql(), name="ck_platform_audit_scope_type"),
        Index("ix_platform_audit_scope_created", "scope_type", "scope_id", "created_at"),
        Index("ix_platform_audit_actor_created", "actor_user_id", "created_at"),
        Index("ix_platform_audit_action_created", "action", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    action: Mapped[str] = mapped_column(String(64))
    scope_type: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    before_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
