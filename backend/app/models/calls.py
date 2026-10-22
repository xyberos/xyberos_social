"""Call records: who called whom, when, and how it ended — never the media.

A call row is metadata. The media is peer-to-peer under DTLS-SRTP and has no server-side
path, so these tables deliberately hold no stream, no recording, and no key material. The
state vocabulary keeps the happy path (`ringing → accepted → ended`) and the terminal
branches (`declined`, `missed`, `busy`, `failed`, `cancelled`) in one `end_reason` column,
so a call cannot be in two states at once.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

CALL_STATES = ("ringing", "accepted", "ended")
CALL_MEDIA = ("audio", "video")
CALL_END_REASONS = ("hangup", "declined", "missed", "busy", "failed", "cancelled")
CALL_PARTICIPANT_STATES = ("invited", "joined", "declined", "left", "missed")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        CheckConstraint(_one_of("media", CALL_MEDIA), name="ck_call_media"),
        CheckConstraint(_one_of("state", CALL_STATES), name="ck_call_state"),
        CheckConstraint(
            f"end_reason IS NULL OR {_one_of('end_reason', CALL_END_REASONS)}",
            name="ck_call_end_reason",
        ),
        CheckConstraint(
            "(state = 'ended' AND ended_at IS NOT NULL) OR (state != 'ended')",
            name="ck_call_ended_at",
        ),
        Index("ix_calls_conversation_started", "conversation_id", "started_at"),
        Index("ix_calls_state_started", "state", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    initiator_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    media: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(8), default="ringing", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)


class CallParticipant(Base):
    __tablename__ = "call_participants"
    __table_args__ = (
        CheckConstraint(
            _one_of("state", CALL_PARTICIPANT_STATES), name="ck_call_participant_state"
        ),
        Index("ix_call_participants_user", "user_id", "call_id"),
    )

    call_id: Mapped[UUID] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(8), default="invited", nullable=False)
    invited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
