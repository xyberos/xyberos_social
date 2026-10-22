"""Attachment metadata for media stored outside PostgreSQL.

The row carries everything needed to authorize, verify, and delete the object: owner, scope,
object key, declared and detected type, size, checksum, scan state, and lifecycle timestamps.
The bytes themselves never live here (Decision 15).

Two lifecycle notes:

* `scan_state` gates downloads. `pending_upload` means a target was issued and nothing has
  been verified; `pending_scan` means the object exists but no verdict does; only `scanned`
  is downloadable, and `rejected` never is.
* `message_id` and `post_id` cascade, so deleting the message or post drops its attachment
  rows. The object is removed by `services/attachments.py` before that happens, because
  object storage cannot cascade — a row that disappears without its object leaves bytes
  nobody can reach.
* A conversation attachment is released once every recipient holds a copy, because the
  point of a chat file is that it was received. A post attachment is not: a post is read by
  whoever arrives later, so its bytes stay until the account removes them.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.scoping import scope_type_check_sql

PENDING_UPLOAD = "pending_upload"
PENDING_SCAN = "pending_scan"
SCANNED = "scanned"
REJECTED = "rejected"

SCAN_STATES = (PENDING_UPLOAD, PENDING_SCAN, SCANNED, REJECTED)


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint(scope_type_check_sql(), name="ck_attachment_scope_type"),
        CheckConstraint(
            "scan_state IN ('pending_upload', 'pending_scan', 'scanned', 'rejected')",
            name="ck_attachment_scan_state",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_attachment_size"),
        CheckConstraint(
            "length(original_filename) <= 200", name="ck_attachment_filename_length"
        ),
        UniqueConstraint("object_key", name="uq_attachment_object_key"),
        Index("ix_attachments_owner_state", "owner_user_id", "scan_state"),
        Index("ix_attachments_message", "message_id"),
        Index("ix_attachments_post", "post_id"),
        Index("ix_attachments_scope_created", "scope_type", "scope_id", "created_at"),
        Index("ix_attachments_expiry", "scan_state", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversation_messages.id", ondelete="CASCADE"), nullable=True
    )
    post_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=True
    )
    object_key: Mapped[str] = mapped_column(String(255))
    original_filename: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    declared_mime_type: Mapped[str] = mapped_column(String(120))
    detected_mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    declared_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scan_state: Mapped[str] = mapped_column(
        String(16), default=PENDING_UPLOAD, nullable=False
    )
    scan_detail: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AttachmentDelivery(Base):
    """One row per participant that has actually fetched an attachment's bytes.

    The stored object is single-copy, so it is removed once every other participant has a
    row here. The attachment row itself stays as a tombstone: the message keeps its place and
    says the file expired, while the reader who already received it can still open their own
    browser-side copy.
    """

    __tablename__ = "attachment_deliveries"
    __table_args__ = (Index("ix_attachment_deliveries_user", "user_id"),)

    attachment_id: Mapped[UUID] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
