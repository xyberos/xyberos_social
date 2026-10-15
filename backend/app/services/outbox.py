"""Writing to the transactional outbox.

`enqueue_event` deliberately does not commit. The row belongs to the caller's transaction
so the event and the state change it announces are one atomic unit; a publisher that reads
unpublished rows can therefore never observe an event whose change was rolled back.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.events import validate_event_name
from app.core.scoping import Scope
from app.models import OutboxEvent


def enqueue_event(
    database: Session,
    *,
    event: str,
    aggregate_type: str,
    aggregate_id: UUID,
    scope: Scope,
    payload: dict[str, Any] | None = None,
) -> OutboxEvent:
    record = OutboxEvent(
        event=validate_event_name(event),
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload or {},
        **scope.columns(),
    )
    database.add(record)
    return record
