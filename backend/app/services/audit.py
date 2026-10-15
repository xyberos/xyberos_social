"""Writing the append-only platform audit log.

Callers add the row to the transaction they are already in, so an audit record and the
change it describes commit together or not at all. The request id is read from the
request context rather than passed in, because every caller already runs inside a request.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.events import validate_event_name
from app.core.observability import current_request_id
from app.core.scoping import Scope, scoped_select
from app.models import PlatformAuditEvent

MAX_AUDIT_PAGE_SIZE = 200


def record_audit(
    database: Session,
    *,
    action: str,
    scope: Scope,
    actor_user_id: UUID | None = None,
    target_type: str | None = None,
    target_id: UUID | None = None,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    reason: str = "",
) -> PlatformAuditEvent:
    record = PlatformAuditEvent(
        action=validate_event_name(action),
        actor_user_id=actor_user_id,
        target_type=target_type,
        target_id=target_id,
        before_state=before_state,
        after_state=after_state,
        reason=reason,
        request_id=current_request_id(),
        **scope.columns(),
    )
    database.add(record)
    return record


def audit_trail(database: Session, scope: Scope, *, limit: int = 50) -> list[PlatformAuditEvent]:
    """Newest-first slice of one scope's trail, bounded so the read cannot grow unbounded."""
    bounded = max(1, min(limit, MAX_AUDIT_PAGE_SIZE))
    return list(
        database.scalars(
            scoped_select(PlatformAuditEvent, scope)
            .order_by(PlatformAuditEvent.created_at.desc(), PlatformAuditEvent.id.desc())
            .limit(bounded)
        )
    )
