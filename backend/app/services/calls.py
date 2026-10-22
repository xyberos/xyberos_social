"""Call records, the ring/join/leave state machine, the ceiling, and the signaling relay.

A call is metadata plus an authorization boundary. The media is a mesh of peer-to-peer
DTLS-SRTP connections, so no audio or video passes through this application; what lives here
is who was invited, who joined, when, and how the call ended. Every state change is therefore
cheap, recorded, and useful for the one thing a caller needs afterwards: a missed-call
notification.

Two rules shape the code. First, blocks are absolute: a blocked pair is never invited into
the same call and their signaling is never relayed. Second, the ceiling is enforced here and
not only in the browser, because a mesh of `n` participants costs each participant `n - 1`
uplinks and each worker `n` sockets.
"""

import base64
import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta
from threading import Lock
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import events
from app.core.config import get_settings
from app.core.metrics import (
    call_rejections,
    call_ring_duration,
    call_signals,
    calls_ended,
    calls_started,
)
from app.core.scoping import conversation_scope
from app.core.security import check_rate_limit
from app.models import Call, CallParticipant, Conversation, ConversationMember, User
from app.schemas.calls import CallPayload, CallParticipantPayload, CallSignal
from app.services.authorization import is_blocked_between
from app.services.messaging import conversation_sockets
from app.services.notifications import queue_notification
from app.services.outbox import enqueue_event

# A browser offer with a full candidate set stays far below this; the cap exists so signaling
# cannot carry anything larger than a call description.
MAX_SIGNAL_BYTES = 64 * 1024


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


class CallRegistry:
    """Per-worker occupancy, so one worker cannot accept more calls than it can signal.

    The database is the record; this set is a load signal. `prune` drops ids that ended in
    another worker, which keeps a long-lived process from drifting upward forever.
    """

    def __init__(self) -> None:
        self._active: set[UUID] = set()
        self._lock = Lock()

    def track(self, call_id: UUID) -> None:
        with self._lock:
            self._active.add(call_id)

    def release(self, call_id: UUID) -> None:
        with self._lock:
            self._active.discard(call_id)

    def count(self) -> int:
        with self._lock:
            return len(self._active)

    def prune(self, database: Session) -> None:
        with self._lock:
            tracked = list(self._active)
        if not tracked:
            return
        live = set(
            database.scalars(
                select(Call.id).where(Call.id.in_(tracked), Call.state != "ended")
            ).all()
        )
        with self._lock:
            self._active &= live


registry = CallRegistry()


def call_rows(database: Session, call_id: UUID) -> list[tuple[CallParticipant, User]]:
    return database.execute(
        select(CallParticipant, User)
        .join(User, User.id == CallParticipant.user_id)
        .where(CallParticipant.call_id == call_id)
        .order_by(CallParticipant.invited_at, User.display_name)
    ).all()


def joined_participants(database: Session, call_id: UUID) -> list[CallParticipant]:
    return list(
        database.scalars(
            select(CallParticipant).where(
                CallParticipant.call_id == call_id,
                CallParticipant.state == "joined",
            )
        ).all()
    )


def call_payload(database: Session, call: Call) -> dict:
    settings = get_settings()
    kind = database.scalar(
        select(Conversation.kind).where(Conversation.id == call.conversation_id)
    )
    rows = call_rows(database, call.id)
    return CallPayload(
        id=str(call.id),
        conversation_id=str(call.conversation_id),
        kind="group" if kind == "group" else "direct",
        media="video" if call.media == "video" else "audio",
        state=call.state,
        initiator_user_id=str(call.initiator_user_id),
        participants=[
            CallParticipantPayload(
                user_id=str(participant.user_id),
                display_name=user.display_name,
                username=user.username,
                state=participant.state,
                joined_at=participant.joined_at.isoformat() if participant.joined_at else None,
            )
            for participant, user in rows
        ],
        started_at=_as_utc(call.started_at).isoformat(),
        answered_at=_as_utc(call.answered_at).isoformat() if call.answered_at else None,
        ended_at=_as_utc(call.ended_at).isoformat() if call.ended_at else None,
        end_reason=call.end_reason,
        expires_at=(
            _as_utc(call.started_at) + timedelta(seconds=settings.call_ring_timeout_seconds)
        ).isoformat(),
    ).model_dump()


async def publish_call_state(database: Session, call: Call) -> None:
    """Push the authoritative call state to every participant of the call."""
    payload = call_payload(database, call)
    for participant, _user in call_rows(database, call.id):
        await conversation_sockets.send_to_user(
            call.conversation_id,
            participant.user_id,
            {"type": "call_state", "call": payload},
        )


async def publish_calls(database: Session, calls: list[Call]) -> None:
    for call in calls:
        await publish_call_state(database, call)


def user_busy(database: Session, user_id: UUID) -> bool:
    """True when the reader is already inside a call that has not ended."""
    return (
        database.scalar(
            select(func.count())
            .select_from(CallParticipant)
            .join(Call, Call.id == CallParticipant.call_id)
            .where(
                CallParticipant.user_id == user_id,
                CallParticipant.state == "joined",
                Call.state.in_(("ringing", "accepted")),
            )
        )
        or 0
    ) > 0


def _active_members(database: Session, conversation_id: UUID, exclude_user_id: UUID) -> list[User]:
    return list(
        database.scalars(
            select(User)
            .join(ConversationMember, ConversationMember.user_id == User.id)
            .where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.left_at.is_(None),
                User.id != exclude_user_id,
                User.is_active.is_(True),
            )
        ).all()
    )


def start_call(database: Session, conversation: Conversation, initiator: User, media: str) -> Call:
    """Create a ringing call and invite everyone who can take it right now."""
    settings = get_settings()
    if media not in ("audio", "video"):
        raise HTTPException(status_code=422, detail="Choose an audio or video call.")
    if user_busy(database, initiator.id):
        call_rejections.inc(reason="busy")
        raise HTTPException(status_code=409, detail="You are already in a call.")
    if not check_rate_limit(
        str(initiator.id),
        "call-start",
        limit=settings.call_start_limit_per_minute,
        seconds=60,
    ):
        call_rejections.inc(reason="rate-limited")
        raise HTTPException(status_code=429, detail="You are starting calls too quickly.")
    registry.prune(database)
    if registry.count() >= settings.call_concurrent_limit:
        call_rejections.inc(reason="capacity")
        raise HTTPException(
            status_code=503, detail="Calls are busy right now. Try again in a moment."
        )

    invitees: list[User] = []
    for candidate in _active_members(database, conversation.id, initiator.id):
        if not candidate.accept_calls:
            continue
        if is_blocked_between(database, initiator.id, candidate.id):
            if conversation.kind == "direct":
                call_rejections.inc(reason="blocked")
                raise HTTPException(
                    status_code=403,
                    detail="Calling is blocked between these accounts.",
                )
            continue
        if user_busy(database, candidate.id):
            continue
        invitees.append(candidate)
    if not invitees:
        call_rejections.inc(reason="unreachable")
        raise HTTPException(
            status_code=409, detail="Nobody in this conversation can take a call right now."
        )
    if len(invitees) + 1 > settings.call_max_participants:
        call_rejections.inc(reason="full")
        raise HTTPException(
            status_code=409,
            detail=(
                "Calls in this conversation are limited to "
                f"{settings.call_max_participants} participants."
            ),
        )

    now = datetime.now(UTC)
    call = Call(
        conversation_id=conversation.id,
        initiator_user_id=initiator.id,
        media=media,
        state="ringing",
        started_at=now,
    )
    database.add(call)
    database.flush()
    database.add(
        CallParticipant(
            call_id=call.id,
            user_id=initiator.id,
            state="joined",
            invited_at=now,
            joined_at=now,
        )
    )
    for invitee in invitees:
        database.add(
            CallParticipant(call_id=call.id, user_id=invitee.id, state="invited", invited_at=now)
        )
    enqueue_event(
        database,
        event=events.CALL_STARTED,
        aggregate_type="call",
        aggregate_id=call.id,
        scope=conversation_scope(conversation.id),
        payload={
            "conversation_id": str(conversation.id),
            "initiator_id": str(initiator.id),
            "media": media,
            "invited": len(invitees),
        },
    )
    calls_started.inc(media=media, kind=conversation.kind)
    registry.track(call.id)
    return call


def accept_call(database: Session, call: Call, user: User) -> Call:
    """Join a ringing or running call as this participant."""
    settings = get_settings()
    participant = database.get(CallParticipant, (call.id, user.id))
    if participant is None:
        raise HTTPException(status_code=404, detail="Call not found.")
    if call.state == "ended":
        call_rejections.inc(reason="ended")
        raise HTTPException(status_code=409, detail="This call has ended.")
    if participant.state == "joined":
        return call
    if participant.state in {"declined", "left", "missed"}:
        call_rejections.inc(reason="left")
        raise HTTPException(status_code=409, detail="You have already left this call.")
    joined = joined_participants(database, call.id)
    if len(joined) + 1 > settings.call_max_participants:
        call_rejections.inc(reason="full")
        raise HTTPException(status_code=409, detail="This call is full.")
    if any(is_blocked_between(database, user.id, other.user_id) for other in joined):
        call_rejections.inc(reason="blocked")
        raise HTTPException(status_code=409, detail="This call is unavailable.")

    now = datetime.now(UTC)
    participant.state = "joined"
    participant.joined_at = now
    participant.left_at = None
    if call.state == "ringing":
        call.state = "accepted"
        call.answered_at = now
        # Join latency as the server sees it: ring to first accepted participant.
        call_ring_duration.observe(
            max(0.0, (now - _as_utc(call.started_at)).total_seconds())
        )
    enqueue_event(
        database,
        event=events.CALL_ACCEPTED,
        aggregate_type="call",
        aggregate_id=call.id,
        scope=conversation_scope(call.conversation_id),
        payload={"conversation_id": str(call.conversation_id), "user_id": str(user.id)},
    )
    return call


def leave_call(database: Session, call: Call, user: User, *, reason: str = "hangup") -> Call:
    """Remove one participant; end the call when the last joined participant leaves."""
    if call.state == "ended":
        return call
    participant = database.get(CallParticipant, (call.id, user.id))
    if participant is None:
        raise HTTPException(status_code=404, detail="Call not found.")
    if participant.state == "joined":
        participant.state = "left"
        participant.left_at = datetime.now(UTC)
        database.flush()
    if not joined_participants(database, call.id):
        _close(database, call, reason=reason)
    return call


def decline_call(database: Session, call: Call, user: User) -> Call:
    """Refuse a ringing invite. Declining a call that is already running leaves it."""
    if call.state == "ended":
        return call
    participant = database.get(CallParticipant, (call.id, user.id))
    if participant is None:
        raise HTTPException(status_code=404, detail="Call not found.")
    if participant.state == "joined":
        return leave_call(database, call, user)
    if participant.state != "invited":
        return call
    participant.state = "declined"
    participant.left_at = datetime.now(UTC)
    database.flush()
    # Ringing ends when nobody is left who could still answer. The record says declined rather
    # than missed: the recipient answered with a decision.
    if call.state == "ringing":
        still_invited = database.scalar(
            select(func.count())
            .select_from(CallParticipant)
            .where(CallParticipant.call_id == call.id, CallParticipant.state == "invited")
        )
        if not still_invited:
            _close(database, call, reason="declined")
    return call


def end_call(database: Session, call: Call, actor: User | None, *, reason: str = "hangup") -> Call:
    """End the whole call for everyone. Idempotent, so a retry cannot double-notify."""
    if call.state == "ended":
        return call
    if call.answered_at is None and reason == "hangup":
        # Nobody ever joined: the caller cancelled a ring rather than hanging up a call.
        reason = "cancelled"
    _close(database, call, reason=reason)
    return call


def expire_stale_calls(database: Session, conversation_id: UUID | None = None) -> list[Call]:
    """Close ring timeouts. Lazy, so no background worker is needed to keep records honest."""
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.call_ring_timeout_seconds)
    statement = select(Call).where(Call.state == "ringing")
    if conversation_id is not None:
        statement = statement.where(Call.conversation_id == conversation_id)
    expired: list[Call] = []
    for call in database.scalars(statement).all():
        if _as_utc(call.started_at) > cutoff:
            continue
        _close(database, call, reason="missed")
        expired.append(call)
    return expired


def _close(database: Session, call: Call, *, reason: str) -> None:
    """Finish a call, record each participant's outcome, and notify whoever missed it."""
    now = datetime.now(UTC)
    call.state = "ended"
    call.ended_at = now
    call.end_reason = reason
    initiator = database.get(User, call.initiator_user_id)
    for participant, participant_user in call_rows(database, call.id):
        if participant.state == "joined":
            participant.state = "left"
            participant.left_at = now
        elif participant.state == "invited":
            participant.state = "missed"
            participant.left_at = now
            if initiator is not None:
                label = "video call" if call.media == "video" else "call"
                queue_notification(
                    database,
                    participant_user,
                    initiator,
                    "call",
                    f"Missed {label} from {initiator.display_name}",
                    f"/conversations/{call.conversation_id}",
                )
    calls_ended.inc(reason=reason)
    registry.release(call.id)
    enqueue_event(
        database,
        event=events.CALL_ENDED,
        aggregate_type="call",
        aggregate_id=call.id,
        scope=conversation_scope(call.conversation_id),
        payload={
            "conversation_id": str(call.conversation_id),
            "reason": reason,
            "media": call.media,
            "answered": call.answered_at is not None,
        },
    )


async def _refuse(conversation_id: UUID, sender_id: UUID, call_id: str, reason: str) -> None:
    await conversation_sockets.send_to_user(
        conversation_id,
        sender_id,
        {"type": "call_error", "call_id": call_id, "reason": reason},
    )


async def relay_call_signal(
    database: Session,
    *,
    conversation_id: UUID,
    sender: User,
    packet: str,
) -> None:
    """Relay one media-negotiation packet between two joined participants of one call."""
    if len(packet.encode("utf-8")) > MAX_SIGNAL_BYTES:
        return
    try:
        signal = CallSignal.model_validate_json(packet)
    except ValidationError:
        # The conversation socket also carries plain-text heartbeats and packets this build
        # does not understand; both are ignored rather than answered.
        return
    call_id = _parse_uuid(signal.call_id)
    target_id = _parse_uuid(signal.to_user_id)
    call = database.get(Call, call_id) if call_id is not None else None
    if (
        call is None
        or target_id is None
        or call.conversation_id != conversation_id
        or call.state != "accepted"
    ):
        call_rejections.inc(reason="signal-unavailable")
        await _refuse(conversation_id, sender.id, signal.call_id, "unavailable")
        return
    sender_row = database.get(CallParticipant, (call.id, sender.id))
    target_row = database.get(CallParticipant, (call.id, target_id))
    if (
        sender_row is None
        or sender_row.state != "joined"
        or target_row is None
        or target_row.state != "joined"
    ):
        call_rejections.inc(reason="signal-unavailable")
        await _refuse(conversation_id, sender.id, signal.call_id, "unavailable")
        return
    if is_blocked_between(database, sender.id, target_id):
        call_rejections.inc(reason="blocked")
        await _refuse(conversation_id, sender.id, signal.call_id, "blocked")
        return
    settings = get_settings()
    if not check_rate_limit(
        str(sender.id),
        f"call-signal:{call.id}",
        limit=settings.call_signal_limit_per_minute,
        seconds=60,
    ):
        call_rejections.inc(reason="rate-limited")
        await _refuse(conversation_id, sender.id, signal.call_id, "rate-limited")
        return
    call_signals.inc(signal=signal.signal)
    await conversation_sockets.send_to_user(
        conversation_id,
        target_id,
        {
            "type": "call_signal",
            "call_id": str(call.id),
            "signal": signal.signal,
            "payload": signal.payload,
            "from_user_id": str(sender.id),
            "from_name": sender.display_name,
            "from_username": sender.username,
        },
    )


def remove_blocked_pair_from_calls(
    database: Session, first_user_id: UUID, second_user_id: UUID
) -> list[Call]:
    """A block is absolute: neither account stays in a call the other is in.

    In a direct conversation this ends the call. In a group call the pair leaves and the
    remaining participants continue, which is the same rule group messages already follow:
    a block is between two accounts, not a veto over everyone else.
    """
    pair = (first_user_id, second_user_id)
    candidate_ids = database.scalars(
        select(Call.id)
        .join(CallParticipant, CallParticipant.call_id == Call.id)
        .where(
            Call.state.in_(("ringing", "accepted")),
            CallParticipant.user_id.in_(pair),
        )
        .distinct()
    ).all()
    now = datetime.now(UTC)
    changed: list[Call] = []
    for call_id in candidate_ids:
        call = database.get(Call, call_id)
        present = set(
            database.scalars(
                select(CallParticipant.user_id).where(CallParticipant.call_id == call_id)
            ).all()
        )
        if not set(pair) <= present:
            continue
        for user_id in pair:
            participant = database.get(CallParticipant, (call_id, user_id))
            if participant is None or participant.state in {"left", "declined", "missed"}:
                continue
            participant.state = "left"
            participant.left_at = now
        database.flush()
        if not joined_participants(database, call_id):
            _close(database, call, reason="hangup")
        changed.append(call)
    return changed


async def leave_call_on_disconnect(
    database: Session, conversation_id: UUID, user_id: UUID
) -> None:
    """Drop a participant whose last page for the conversation closed.

    Signaling rides that socket, so a participant without one cannot negotiate, mute, or
    leave; keeping them "joined" would make the record lie. A participant with another tab
    open stays in the call.
    """
    if conversation_sockets.is_connected(conversation_id, user_id):
        return
    call = database.scalar(
        select(Call)
        .join(CallParticipant, CallParticipant.call_id == Call.id)
        .where(
            Call.conversation_id == conversation_id,
            Call.state.in_(("ringing", "accepted")),
            CallParticipant.user_id == user_id,
            CallParticipant.state == "joined",
        )
        .order_by(Call.started_at.desc())
        .limit(1)
    )
    if call is None:
        return
    user = database.get(User, user_id)
    if user is None:
        return
    leave_call(database, call, user)
    database.commit()
    await publish_call_state(database, call)


def ice_servers_for(user_id: UUID) -> list[dict]:
    """STUN plus, when a relay is configured, per-session TURN credentials."""
    settings = get_settings()
    servers: list[dict] = [
        {"urls": url.strip()} for url in settings.webrtc_ice_servers.split(",") if url.strip()
    ]
    turn_urls = [url.strip() for url in settings.webrtc_turn_urls.split(",") if url.strip()]
    if not turn_urls or not settings.webrtc_turn_secret:
        # A TURN URL without a shared secret would need a static credential, which the plan
        # rules out; leaving it out is the honest failure.
        return servers
    expires_at = int(time.time()) + settings.webrtc_turn_ttl_seconds
    username = f"{expires_at}:{user_id}"
    credential = base64.b64encode(
        hmac.new(
            settings.webrtc_turn_secret.encode("utf-8"),
            username.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("ascii")
    servers.append({"urls": turn_urls, "username": username, "credential": credential})
    return servers


def active_call_totals(database: Session) -> tuple[int, int]:
    """Open calls and their joined participants, for the scrape-time gauges."""
    open_calls = database.scalar(
        select(func.count()).select_from(Call).where(Call.state.in_(("ringing", "accepted")))
    )
    joined = database.scalar(
        select(func.count())
        .select_from(CallParticipant)
        .join(Call, Call.id == CallParticipant.call_id)
        .where(CallParticipant.state == "joined", Call.state.in_(("ringing", "accepted")))
    )
    return int(open_calls or 0), int(joined or 0)
