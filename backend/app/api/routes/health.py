"""Liveness, readiness, and the metrics scrape endpoint."""

import logging
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from app.core.database import engine, get_db
from app.api.deps import authenticated_user
from app.core.metrics import gauge, registry, uptime_seconds
from app.models import IdempotencyKey, OutboxEvent
from app.services.attachments import pending_upload_count
from app.services.calls import active_call_totals

router = APIRouter()

logger = logging.getLogger(__name__)


@router.get("/api/live/revision", include_in_schema=False)
def live_revision(request: Request, database: Session = Depends(get_db)):
    """A content free revision so every worker can notice committed outbox activity."""
    user = authenticated_user(request, database)
    if user is None:
        return JSONResponse({"detail": "Authentication required."}, status_code=401)
    revision = database.scalar(select(func.count()).select_from(OutboxEvent)) or 0
    return {"revision": revision}

_POOL_STATES = (
    ("size", "size"),
    ("checked_in", "checkedin"),
    ("checked_out", "checkedout"),
    ("overflow", "overflow"),
)


@router.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness only: no dependencies, so a database outage never triggers a restart."""
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readyz(database: Session = Depends(get_db)):
    try:
        database.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("readiness check failed", extra={"event": "readiness.failed"})
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}


def _pool_samples() -> list[tuple[dict[str, object], float]]:
    """Pool saturation, so pool exhaustion is visible before it becomes a timeout."""
    samples: list[tuple[dict[str, object], float]] = []
    for state, accessor_name in _POOL_STATES:
        accessor = getattr(engine.pool, accessor_name, None)
        if not callable(accessor):
            continue
        try:
            samples.append(({"state": state}, float(accessor())))
        except (NotImplementedError, TypeError):
            # A pool without bookkeeping, such as the static pool used in tests.
            continue
    return samples


def _queue_depth(database: Session, statement) -> float:
    return float(database.scalar(statement) or 0)


@router.get("/metrics", include_in_schema=False)
def metrics(database: Session = Depends(get_db)):
    """Aggregate counters and saturation gauges for a scraper.

    A failed database read drops the queue gauges and still answers 200: metrics matter
    most exactly when the database is unreachable, so this endpoint must not depend on it.
    """
    blocks = [
        gauge(
            "soccom_uptime_seconds",
            "Seconds since the process started.",
            [({}, uptime_seconds())],
        ),
        gauge(
            "soccom_db_pool_connections",
            "Database pool connections by state.",
            _pool_samples(),
        ),
    ]
    try:
        blocks.append(
            gauge(
                "soccom_outbox_unpublished_events",
                "Outbox events written but not yet published.",
                [
                    (
                        {},
                        _queue_depth(
                            database,
                            select(func.count())
                            .select_from(OutboxEvent)
                            .where(OutboxEvent.published_at.is_(None)),
                        ),
                    )
                ],
            )
        )
        blocks.append(
            gauge(
                "soccom_idempotency_keys_in_progress",
                "Idempotency keys reserved by a request that has not finished.",
                [
                    (
                        {},
                        _queue_depth(
                            database,
                            select(func.count())
                            .select_from(IdempotencyKey)
                            .where(IdempotencyKey.state == "in_progress"),
                        ),
                    )
                ],
            )
        )
        blocks.append(
            gauge(
                "soccom_attachments_unverified",
                "Attachments issued or uploaded but not yet verified by a scan.",
                [({}, float(pending_upload_count(database)))],
            )
        )
        open_calls, joined_participants = active_call_totals(database)
        blocks.append(
            gauge(
                "soccom_calls_active",
                "Calls ringing or in progress.",
                [({}, float(open_calls))],
            )
        )
        blocks.append(
            gauge(
                "soccom_call_participants_joined",
                "Participants currently joined to an open call.",
                [({}, float(joined_participants))],
            )
        )
    except SQLAlchemyError:
        logger.warning("metrics database gauges unavailable", extra={"event": "metrics.degraded"})
    return Response(
        registry.render(blocks), media_type="text/plain; version=0.0.4; charset=utf-8"
    )
