"""Idempotent handling of retried mutations.

A client that may retry a mutating request sends an `Idempotency-Key` header, or carries
the same value in an `idempotency_key` form field when the request comes from a
server-rendered form. The first request reserves the key, runs, and stores its outcome in
the same transaction as the change. A second request with the same key and the same
request content replays that outcome without running the mutation again; the same key with
different content is rejected instead of being silently applied to the wrong request.

Keys are scoped to the actor that presented them, so one account cannot read or block
another account's key. Retention of finished keys is a later concern: nothing expires them
until the publisher that consumes the outbox also prunes this table.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import IdempotencyKey

IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_FIELD = "idempotency_key"
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_IN_PROGRESS_DETAIL = "A request with this idempotency key is still being processed."
_REUSED_DETAIL = "This idempotency key was already used for a different request."


@dataclass(frozen=True)
class Replay:
    """The stored outcome of the request that first used the key."""

    status_code: int
    location: str | None = None
    body: dict | None = None


@dataclass(frozen=True)
class Reservation:
    key: str
    record: IdempotencyKey | None = None
    replay: Replay | None = None

    @property
    def replayed(self) -> bool:
        return self.replay is not None


def actor_scope(user_id: UUID | None) -> str:
    return f"account:{user_id}" if user_id is not None else "anonymous"


def request_fingerprint(*parts: object) -> str:
    """Digest of the request content a key must not be reused across."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def presented_key(request: Request, form_value: str | None = None) -> str | None:
    candidate = request.headers.get(IDEMPOTENCY_HEADER) or form_value
    if candidate is None:
        return None
    candidate = candidate.strip()
    if not _KEY_PATTERN.match(candidate):
        raise HTTPException(
            status_code=400,
            detail="The idempotency key must be 8-128 characters of letters, digits, . _ : or -.",
        )
    return candidate


def begin(
    database: Session,
    *,
    scope: str,
    key: str,
    endpoint: str,
    fingerprint: str,
) -> Reservation:
    """Reserve the key, or report the outcome the first request already produced."""
    existing = _find(database, scope, key)
    if existing is not None:
        return _interpret(existing, fingerprint)

    record = IdempotencyKey(
        scope=scope,
        key=key,
        endpoint=endpoint,
        request_fingerprint=fingerprint,
    )
    try:
        # The savepoint keeps a lost race from discarding the caller's other work.
        with database.begin_nested():
            database.add(record)
    except IntegrityError:
        winner = _find(database, scope, key)
        if winner is None:
            raise
        return _interpret(winner, fingerprint)
    return Reservation(key=key, record=record)


def replay_response(replay: Replay) -> Response:
    """Rebuild the stored outcome so a replay is indistinguishable from the first reply."""
    if replay.body is not None:
        return JSONResponse(replay.body, status_code=replay.status_code)
    return RedirectResponse(replay.location or "/", status_code=replay.status_code)


def complete(
    database: Session,
    reservation: Reservation,
    *,
    status_code: int,
    location: str | None = None,
    body: dict | None = None,
) -> None:
    """Store the outcome so a replay returns it; commits with the caller's transaction."""
    if reservation.record is None:
        return
    reservation.record.state = "completed"
    reservation.record.response_status = status_code
    reservation.record.response_location = location
    reservation.record.response_body = json.dumps(body) if body is not None else None
    reservation.record.completed_at = datetime.now(UTC)


def _find(database: Session, scope: str, key: str) -> IdempotencyKey | None:
    return database.scalar(
        select(IdempotencyKey).where(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
    )


def _interpret(existing: IdempotencyKey, fingerprint: str) -> Reservation:
    if existing.request_fingerprint != fingerprint:
        raise HTTPException(status_code=409, detail=_REUSED_DETAIL)
    if existing.state != "completed":
        raise HTTPException(status_code=409, detail=_IN_PROGRESS_DETAIL)
    return Reservation(
        key=existing.key,
        replay=Replay(
            status_code=existing.response_status or 303,
            location=existing.response_location,
            body=json.loads(existing.response_body) if existing.response_body else None,
        ),
    )
