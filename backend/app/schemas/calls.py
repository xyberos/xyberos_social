"""JSON contracts for call records and signaling.

The call payload is the authoritative state every participant's page renders; the client
never derives state from its own local guesses. Signaling packets carry SDP and ICE only.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class CallParticipantPayload(BaseModel):
    user_id: str
    display_name: str
    username: str
    state: str
    joined_at: str | None = None


class CallPayload(BaseModel):
    id: str
    conversation_id: str
    kind: Literal["direct", "group"]
    media: Literal["audio", "video"]
    state: Literal["ringing", "accepted", "ended"]
    initiator_user_id: str
    participants: list[CallParticipantPayload]
    started_at: str
    answered_at: str | None = None
    ended_at: str | None = None
    end_reason: str | None = None
    expires_at: str


class CallStartRequest(BaseModel):
    media: Literal["audio", "video"] = "audio"


class CallEndRequest(BaseModel):
    """Why a participant is ending the call; `failed` is the browser's own verdict."""

    reason: Literal["hangup", "failed"] = "hangup"


class CallSignal(BaseModel):
    """One opaque media-negotiation packet relayed between two joined participants."""

    type: Literal["call_signal"]
    call_id: str
    to_user_id: str
    signal: Literal["offer", "answer", "ice"]
    payload: dict[str, Any] = Field(default_factory=dict)


class IceServersResponse(BaseModel):
    ice_servers: list[dict[str, Any]]
