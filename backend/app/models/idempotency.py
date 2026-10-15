"""Recorded idempotency keys for mutating requests.

A retry that reuses a key must not repeat the change. The row is reserved before the
mutation runs and completed with the resulting response, so a replay returns the original
outcome instead of acting twice, and a reused key with a different request is rejected
rather than silently accepted.

The key is scoped to the actor that sent it, so one account can never read or block
another account's key.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        CheckConstraint("state IN ('in_progress', 'completed')", name="ck_idempotency_state"),
        UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
        Index("ix_idempotency_created", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scope: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(String(128))
    endpoint: Mapped[str] = mapped_column(String(120))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(12), default="in_progress", nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_location: Mapped[str | None] = mapped_column(String(320), nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
