"""The transactional outbox.

An outbox row is written in the same transaction as the state change it announces, and it
is never published before that transaction commits. That rule is what makes a future
publisher at-least-once safe: the event exists if and only if the change exists.

No publisher or worker exists yet, so `published_at` stays NULL until one does; a row is
claimable only while it is NULL.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    JSON,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.scoping import scope_type_check_sql


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(scope_type_check_sql(), name="ck_outbox_scope_type"),
        Index("ix_outbox_unpublished", "published_at", "created_at"),
        Index("ix_outbox_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event: Mapped[str] = mapped_column(String(64))
    scope_type: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    aggregate_type: Mapped[str] = mapped_column(String(32))
    aggregate_id: Mapped[UUID] = mapped_column(Uuid)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
