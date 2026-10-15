"""Directed follow and block edges between accounts."""

from datetime import datetime

from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)

from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UserRelationship(Base):
    __tablename__ = "user_relationships"
    __table_args__ = (
        CheckConstraint("source_user_id != target_user_id", name="ck_relationship_not_self"),
        CheckConstraint("kind IN ('follow', 'block')", name="ck_relationship_kind"),
        Index("ix_relationship_target_kind", "target_user_id", "kind"),
    )

    source_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    target_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(8), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
