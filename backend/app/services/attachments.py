"""The attachment pipeline: issue a target, verify what arrives, authorize delivery.

Nothing here trusts the client. The declared type is compared against the detected one, the
size ceiling is enforced by the storage service and re-checked here, and a download is
authorized by scope membership rather than by knowing a key or a URL.

Every state transition is committed as it happens. That costs an extra commit per finalize
and buys the property this phase is built on: at no point can a failure leave an object both
present and believed clean, and a retry after a scanner outage resumes from `pending_scan`
instead of starting over.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.media import (
    ACCEPTED_TYPES,
    MediaType,
    UnsupportedMedia,
    is_accepted,
    strip_image_metadata,
)
from app.core.scanning import ScanUnavailable, get_scanner
from app.core.scoping import Scope
from app.core.storage import StorageError, get_storage
from app.models import Attachment, ConversationMember, User
from app.models.attachments import PENDING_SCAN, PENDING_UPLOAD, REJECTED, SCANNED
from app.schemas.attachments import AttachmentPayload
from app.services.authorization import is_blocked_between

logger = logging.getLogger(__name__)

CONVERSATION = "conversation"
REAP_BATCH = 50
FILENAME_LIMIT = 200


@dataclass(frozen=True)
class UploadTarget:
    attachment_id: UUID
    upload_url: str
    fields: dict[str, str]
    expires_in: int
    max_bytes: int
    max_attachments: int


def clean_filename(candidate: str) -> str:
    """Keep a display name only: no path components, no control characters."""
    base = (candidate or "").replace("\\", "/").rsplit("/", 1)[-1]
    printable = "".join(character for character in base if character.isprintable()).strip()
    return printable[:FILENAME_LIMIT] or "attachment"


def object_key(scope: Scope, attachment_id: UUID) -> str:
    """Derived from scope and id, so a client can never choose where its bytes land."""
    tenant = scope.id if scope.id is not None else "platform"
    return f"attachments/{scope.kind}/{tenant}/{attachment_id}"


def quota_used(database: Session, owner_user_id: UUID) -> int:
    """Bytes this account has stored, counting what is verified and what is still being scanned."""
    total = database.scalar(
        select(func.coalesce(func.sum(Attachment.size_bytes), 0)).where(
            Attachment.owner_user_id == owner_user_id,
            Attachment.scan_state.in_((PENDING_SCAN, SCANNED)),
            Attachment.deleted_at.is_(None),
        )
    )
    return int(total or 0)


def issue_upload_target(
    database: Session,
    *,
    user: User,
    scope: Scope,
    declared_mime_type: str,
    size_bytes: int,
    filename: str = "",
) -> UploadTarget:
    settings = get_settings()
    declared = (declared_mime_type or "").strip().lower()
    if not is_accepted(declared):
        accepted = ", ".join(sorted(ACCEPTED_TYPES))
        raise HTTPException(
            status_code=415, detail=f"That file type is not accepted. Accepted: {accepted}."
        )
    if size_bytes <= 0:
        raise HTTPException(status_code=422, detail="The upload must not be empty.")
    if size_bytes > settings.attachment_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Attachments are limited to {settings.attachment_max_bytes // (1024 * 1024)} MB.",
        )
    used = quota_used(database, user.id)
    if used + size_bytes > settings.attachment_account_quota_bytes:
        raise HTTPException(
            status_code=409,
            detail="Attachment storage is full for this account. Delete something to free space.",
        )

    reap_expired_uploads(database)

    settings_expiry = datetime.now(UTC) + timedelta(seconds=settings.attachment_upload_ttl_seconds)
    attachment_id = uuid4()
    key = object_key(scope, attachment_id)
    target = get_storage().presign_post(
        key,
        content_type=declared,
        max_bytes=settings.attachment_max_bytes,
        expires_in=settings.attachment_upload_ttl_seconds,
    )
    database.add(
        Attachment(
            id=attachment_id,
            owner_user_id=user.id,
            object_key=key,
            original_filename=clean_filename(filename),
            declared_mime_type=declared,
            declared_size_bytes=size_bytes,
            size_bytes=size_bytes,
            scan_state=PENDING_UPLOAD,
            expires_at=settings_expiry,
            **scope.columns(),
        )
    )
    database.flush()
    return UploadTarget(
        attachment_id=attachment_id,
        upload_url=target["url"],
        fields={name: str(value) for name, value in target["fields"].items()},
        expires_in=settings.attachment_upload_ttl_seconds,
        max_bytes=settings.attachment_max_bytes,
        max_attachments=settings.attachment_message_limit,
    )


def _reject(database: Session, attachment: Attachment, detail: str) -> None:
    """Mark an upload unusable and remove its bytes; a rejected object is never kept."""
    attachment.scan_state = REJECTED
    attachment.scan_detail = detail[:240]
    attachment.finalized_at = datetime.now(UTC)
    try:
        get_storage().delete(attachment.object_key)
    except StorageError:
        logger.warning(
            "could not remove a rejected object",
            extra={"event": "attachment.reject_object_failed", "attachment_id": str(attachment.id)},
        )
    database.commit()


def finalize_upload(database: Session, *, attachment_id: UUID, user: User) -> Attachment:
    """Verify and scan an uploaded object, then promote or reject it. Safe to replay."""
    settings = get_settings()
    attachment = database.get(Attachment, attachment_id)
    if attachment is None or attachment.owner_user_id != user.id:
        raise HTTPException(status_code=404, detail="Upload not found.")
    if attachment.scan_state == SCANNED:
        return attachment
    if attachment.scan_state == REJECTED:
        raise HTTPException(status_code=422, detail=attachment.scan_detail or "Upload rejected.")

    storage = get_storage()
    stored = storage.head(attachment.object_key)
    if stored is None or stored.size == 0:
        raise HTTPException(status_code=409, detail="The upload has not arrived yet. Retry.")

    attachment.scan_state = PENDING_SCAN
    attachment.uploaded_at = datetime.now(UTC)
    database.commit()

    if stored.size > settings.attachment_max_bytes:
        _reject(
            database,
            attachment,
            f"the upload is {stored.size} bytes, over the {settings.attachment_max_bytes} byte limit",
        )
        raise HTTPException(
            status_code=413,
            detail=f"Attachments are limited to {settings.attachment_max_bytes // (1024 * 1024)} MB.",
        )

    payload = storage.get_bytes(attachment.object_key)
    if len(payload) != stored.size:
        _reject(database, attachment, "the object changed while it was being verified")
        raise HTTPException(status_code=422, detail="The upload could not be verified.")

    try:
        verdict = get_scanner().scan(payload, declared_mime=attachment.declared_mime_type)
    except ScanUnavailable:
        # Deliberately not a rejection: the bytes are unverified, not known-bad, so the row
        # stays in pending_scan and the client may retry.
        raise HTTPException(
            status_code=503, detail="Uploads are being scanned; retry in a moment."
        ) from None
    if not verdict.clean:
        _reject(database, attachment, verdict.detail or "the scan rejected the upload")
        raise HTTPException(status_code=422, detail="The upload was rejected by the scanner.")

    if attachment.declared_mime_type.startswith("image/"):
        try:
            payload = strip_image_metadata(payload, attachment.declared_mime_type)
        except UnsupportedMedia:
            _reject(database, attachment, "the image could not be normalized")
            raise HTTPException(status_code=422, detail="The image could not be processed.") from None
        storage.put_bytes(
            attachment.object_key, payload, content_type=attachment.declared_mime_type
        )

    attachment.detected_mime_type = attachment.declared_mime_type
    attachment.size_bytes = len(payload)
    attachment.checksum_sha256 = hashlib.sha256(payload).hexdigest()
    attachment.scan_state = SCANNED
    attachment.scan_detail = ""
    attachment.finalized_at = datetime.now(UTC)
    database.commit()
    return attachment


def attachment_for_download(database: Session, *, attachment_id: UUID, user: User) -> Attachment:
    """Fail closed on every condition: unknown scope, missing membership, block, or no verdict."""
    attachment = database.get(Attachment, attachment_id)
    if (
        attachment is None
        or attachment.deleted_at is not None
        or attachment.scan_state != SCANNED
        or attachment.scope_type != CONVERSATION
    ):
        raise HTTPException(status_code=404, detail="Attachment not found.")
    membership = database.get(ConversationMember, (attachment.scope_id, user.id))
    if membership is None or membership.left_at is not None:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    if is_blocked_between(database, user.id, attachment.owner_user_id):
        raise HTTPException(status_code=404, detail="Attachment not found.")
    return attachment


def media_type_for(attachment: Attachment) -> MediaType:
    return ACCEPTED_TYPES[attachment.detected_mime_type or attachment.declared_mime_type]


def attachment_payload(attachment: Attachment) -> AttachmentPayload:
    """The client-facing shape: verified metadata plus a route, never a storage URL."""
    media = media_type_for(attachment)
    return AttachmentPayload(
        id=str(attachment.id),
        filename=attachment.original_filename,
        content_type=media.mime,
        size_bytes=attachment.size_bytes,
        inline=media.inline,
        download_url=f"/attachments/{attachment.id}/download",
    )


def attachments_for_message(database: Session, message_id: UUID) -> list[Attachment]:
    return list(
        database.scalars(
            select(Attachment)
            .where(Attachment.message_id == message_id, Attachment.scan_state == SCANNED)
            .order_by(Attachment.created_at)
        )
    )


def attach_to_message(
    database: Session, *, message_id: UUID, conversation_id: UUID, user: User, attachment_ids: list[UUID]
) -> list[Attachment]:
    """Bind finalized uploads to a message. Only verified, unattached, own uploads qualify."""
    settings = get_settings()
    ordered = list(attachment_ids)
    if len(ordered) > settings.attachment_message_limit:
        raise HTTPException(
            status_code=422,
            detail=f"A message can carry {settings.attachment_message_limit} attachments.",
        )
    if len(set(ordered)) != len(ordered):
        raise HTTPException(status_code=422, detail="An attachment was listed twice.")
    attachments: list[Attachment] = []
    for attachment_id in ordered:
        attachment = database.get(Attachment, attachment_id)
        if (
            attachment is None
            or attachment.owner_user_id != user.id
            or attachment.scan_state != SCANNED
            or attachment.message_id is not None
            or attachment.deleted_at is not None
            or attachment.scope_type != CONVERSATION
            or attachment.scope_id != conversation_id
        ):
            raise HTTPException(
                status_code=422, detail="One of the attachments cannot be attached to this message."
            )
        attachments.append(attachment)
    for attachment in attachments:
        attachment.message_id = message_id
    return attachments


def delete_attachments(database: Session, attachments: list[Attachment]) -> int:
    """Remove objects first, then rows: a row without its object only hides unreachable bytes."""
    if not attachments:
        return 0
    keys = [attachment.object_key for attachment in attachments]
    try:
        get_storage().delete_many(keys)
    except StorageError:
        # Revoking access matters more than reclaiming the bytes, so the rows still go and
        # the failure is logged for an operator to reconcile.
        logger.warning(
            "could not remove attachment objects",
            extra={"event": "attachment.delete_object_failed", "objects": len(keys)},
        )
    for attachment in attachments:
        database.delete(attachment)
    return len(attachments)


def delete_attachments_for_owner(database: Session, *, owner_user_id: UUID) -> int:
    attachments = list(
        database.scalars(select(Attachment).where(Attachment.owner_user_id == owner_user_id))
    )
    return delete_attachments(database, attachments)


def reap_expired_uploads(database: Session, *, limit: int = REAP_BATCH) -> int:
    """Delete abandoned uploads: anything unverified whose target has expired.

    Called when a new target is issued because there is no worker yet; a scheduler should
    call it as well once one exists.
    """
    now = datetime.now(UTC)
    stale = list(
        database.scalars(
            select(Attachment)
            .where(
                Attachment.scan_state.in_((PENDING_UPLOAD, PENDING_SCAN)),
                Attachment.expires_at.is_not(None),
                Attachment.expires_at < now,
            )
            .order_by(Attachment.expires_at)
            .limit(limit)
        )
    )
    return delete_attachments(database, stale)


def pending_upload_count(database: Session) -> int:
    total = database.scalar(
        select(func.count())
        .select_from(Attachment)
        .where(Attachment.scan_state.in_((PENDING_UPLOAD, PENDING_SCAN)))
    )
    return int(total or 0)
