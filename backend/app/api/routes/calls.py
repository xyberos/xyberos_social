"""Call creation, participant decisions, ICE configuration, and call resync.

Ringing is a server decision: a call record exists before any SDP is exchanged, so every
later signaling packet is authorized against a row instead of against a client-chosen id.
Media negotiation stays on the conversation WebSocket and never touches these routes.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import authenticated_user
from app.core.database import get_db
from app.models import Call, CallParticipant, Conversation, User
from app.schemas.calls import CallEndRequest, CallStartRequest, IceServersResponse
from app.services import calls as call_service
from app.services.messaging import active_conversation_member

router = APIRouter()


def _signed_in(request: Request, database: Session) -> User:
    user = authenticated_user(request, database)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def _call_for_participant(database: Session, call_id: UUID, user: User) -> Call:
    """A call is only visible to the people invited to it; everyone else sees a 404."""
    call = database.get(Call, call_id)
    if call is None or database.get(CallParticipant, (call.id, user.id)) is None:
        raise HTTPException(status_code=404, detail="Call not found.")
    return call


@router.get("/calls/ice", response_model=IceServersResponse)
def ice_servers(request: Request, database: Session = Depends(get_db)):
    """STUN, plus short-lived TURN credentials when a relay is configured."""
    user = _signed_in(request, database)
    return IceServersResponse(ice_servers=call_service.ice_servers_for(user.id))


@router.post("/conversations/{conversation_id}/calls", status_code=201)
async def create_call(
    conversation_id: UUID,
    payload: CallStartRequest,
    request: Request,
    database: Session = Depends(get_db),
):
    user = _signed_in(request, database)
    active_conversation_member(database, conversation_id, user.id)
    conversation = database.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    expired = call_service.expire_stale_calls(database, conversation.id)
    call = call_service.start_call(database, conversation, user, payload.media)
    database.commit()
    await call_service.publish_calls(database, expired)
    await call_service.publish_call_state(database, call)
    return call_service.call_payload(database, call)


@router.get("/conversations/{conversation_id}/calls/current")
async def current_call(
    conversation_id: UUID,
    request: Request,
    database: Session = Depends(get_db),
):
    """The call this reader is inside right now, so a reconnect can resume its UI."""
    user = _signed_in(request, database)
    active_conversation_member(database, conversation_id, user.id)
    expired = call_service.expire_stale_calls(database, conversation_id)
    if expired:
        database.commit()
        await call_service.publish_calls(database, expired)
    call = database.scalar(
        select(Call)
        .join(CallParticipant, CallParticipant.call_id == Call.id)
        .where(
            Call.conversation_id == conversation_id,
            Call.state.in_(("ringing", "accepted")),
            CallParticipant.user_id == user.id,
            CallParticipant.state.in_(("joined", "invited")),
        )
        .order_by(Call.started_at.desc())
        .limit(1)
    )
    return {"call": call_service.call_payload(database, call) if call else None}


@router.get("/calls/{call_id}")
def call_detail(call_id: UUID, request: Request, database: Session = Depends(get_db)):
    user = _signed_in(request, database)
    call = _call_for_participant(database, call_id, user)
    return call_service.call_payload(database, call)


@router.post("/calls/{call_id}/accept")
async def accept_call(call_id: UUID, request: Request, database: Session = Depends(get_db)):
    user = _signed_in(request, database)
    call = _call_for_participant(database, call_id, user)
    expired = call_service.expire_stale_calls(database, call.conversation_id)
    if expired:
        database.commit()
        await call_service.publish_calls(database, expired)
    call_service.accept_call(database, call, user)
    database.commit()
    await call_service.publish_call_state(database, call)
    return call_service.call_payload(database, call)


@router.post("/calls/{call_id}/decline")
async def decline_call(call_id: UUID, request: Request, database: Session = Depends(get_db)):
    user = _signed_in(request, database)
    call = _call_for_participant(database, call_id, user)
    expired = call_service.expire_stale_calls(database, call.conversation_id)
    if expired:
        database.commit()
        await call_service.publish_calls(database, expired)
    call_service.decline_call(database, call, user)
    database.commit()
    await call_service.publish_call_state(database, call)
    return call_service.call_payload(database, call)


@router.post("/calls/{call_id}/leave")
async def leave_call(call_id: UUID, request: Request, database: Session = Depends(get_db)):
    user = _signed_in(request, database)
    call = _call_for_participant(database, call_id, user)
    call_service.leave_call(database, call, user)
    database.commit()
    await call_service.publish_call_state(database, call)
    return call_service.call_payload(database, call)


@router.post("/calls/{call_id}/end")
async def end_call(
    call_id: UUID,
    request: Request,
    payload: CallEndRequest | None = None,
    database: Session = Depends(get_db),
):
    user = _signed_in(request, database)
    call = _call_for_participant(database, call_id, user)
    reason = payload.reason if payload is not None else "hangup"
    call_service.end_call(database, call, user, reason=reason)
    database.commit()
    await call_service.publish_call_state(database, call)
    return call_service.call_payload(database, call)
