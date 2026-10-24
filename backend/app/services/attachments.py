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
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.media import (
    ACCEPTED_TYPES,
    AUDIO,
    DOCUMENT,
    IMAGE,
    MEDIA_CONTENT_TYPES,
    THUMBNAIL_MIME,
    VIDEO,
    MediaType,
    UnsupportedMedia,
    is_accepted,
    kind_for,
    render_thumbnail,
    strip_image_metadata,
)
from app.core.scanning import ScanUnavailable, ScanVerdict, get_scanner
from app.core.scoping import ACCOUNT, Scope
from app.core.storage import StorageError, get_storage
from app.models import (
    Attachment,
    AttachmentDelivery,
    Community,
    ConversationMember,
    Post,
    User,
)
from app.models.attachments import PENDING_SCAN, PENDING_UPLOAD, REJECTED, SCANNED
from app.schemas.attachments import (
    AttachmentPayload,
    MultipartPartPayload,
    MultipartPlanPayload,
    MultipartSessionResponse,
    UploadTargetResponse,
)
from app.services.authorization import (
    can_read_personal_post,
    community_role,
    is_blocked_between,
)

logger = logging.getLogger(__name__)

CONVERSATION = "conversation"
REAP_BATCH = 50
FILENAME_LIMIT = 200
# Enough of a container to name it, matching the scanner's sample size.
SNIFF_BYTES = 512 * 1024
# S3 refuses a smaller part than this, and the last part of an upload is the only one that
# may be shorter.
MIN_PART_BYTES = 5 * 1024 * 1024
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class MultipartPlan:
    """What the client needs to split its file: how large a part is, and how many there are."""

    part_size: int
    part_count: int


@dataclass(frozen=True)
class MultipartSession:
    upload_id: str
    part_size: int
    # One entry per part: number, offset in the file, size of this part, and its URL.
    parts: list[dict[str, int | str]] = field(default_factory=list)
    expires_in: int = 0


@dataclass(frozen=True)
class UploadTarget:
    attachment_id: UUID
    upload_url: str
    fields: dict[str, str]
    expires_in: int
    max_bytes: int
    max_attachments: int
    # True when the bytes were already in storage and were copied server-side, so the client
    # has nothing to send.
    reused: bool = False
    multipart: MultipartPlan | None = None


def _megabytes(value: int) -> int:
    return max(1, value // (1024 * 1024))


def media_ceiling(mime: str, *, scope_kind: str) -> int:
    """The size a single upload of this type may reach, which depends on where it will live.

    A chat attachment keeps the one ceiling the conversation phase set. A post's media is
    read by whoever arrives later and is never released, so it is measured per kind: a photo
    is small, a short video is not, and a song sits between.
    """
    settings = get_settings()
    if scope_kind == CONVERSATION:
        return settings.attachment_max_bytes
    kind = kind_for(mime)
    if kind == VIDEO:
        return settings.post_video_max_bytes
    if kind == AUDIO:
        return settings.post_audio_max_bytes
    # Images and documents share the smaller ceiling; a document is read, not played.
    return settings.post_image_max_bytes


def _limit_detail(mime: str, limit: int) -> str:
    label = {
        IMAGE: "Photos",
        VIDEO: "Videos",
        AUDIO: "Audio files",
        DOCUMENT: "Files",
    }.get(kind_for(mime) or "", "Uploads")
    return f"{label} are limited to {_megabytes(limit)} MB."


def max_items_for(scope_kind: str) -> int:
    settings = get_settings()
    return (
        settings.post_media_limit
        if scope_kind == ACCOUNT
        else settings.attachment_message_limit
    )


@dataclass(frozen=True)
class MediaLimits:
    """What the composer may attach, so the page states the same numbers the server applies."""

    max_items: int
    image_max_bytes: int
    video_max_bytes: int
    audio_max_bytes: int
    accept: str

    @property
    def image_mb(self) -> int:
        return _megabytes(self.image_max_bytes)

    @property
    def video_mb(self) -> int:
        return _megabytes(self.video_max_bytes)

    @property
    def audio_mb(self) -> int:
        return _megabytes(self.audio_max_bytes)

    @property
    def summary(self) -> str:
        return (
            f"Up to {self.max_items} files. Photos to {self.image_mb} MB, "
            f"video to {self.video_mb} MB, audio to {self.audio_mb} MB."
        )


def composer_limits() -> MediaLimits:
    settings = get_settings()
    return MediaLimits(
        max_items=settings.post_media_limit,
        image_max_bytes=settings.post_image_max_bytes,
        video_max_bytes=settings.post_video_max_bytes,
        audio_max_bytes=settings.post_audio_max_bytes,
        accept=",".join(MEDIA_CONTENT_TYPES),
    )


def clean_filename(candidate: str) -> str:
    """Keep a display name only: no path components, no control characters."""
    base = (candidate or "").replace("\\", "/").rsplit("/", 1)[-1]
    printable = "".join(character for character in base if character.isprintable()).strip()
    return printable[:FILENAME_LIMIT] or "attachment"


def object_key(scope: Scope, attachment_id: UUID) -> str:
    """Derived from scope and id, so a client can never choose where its bytes land."""
    tenant = scope.id if scope.id is not None else "platform"
    return f"attachments/{scope.kind}/{tenant}/{attachment_id}"


def thumbnail_key(object_key_value: str) -> str:
    """The derivative lives beside its original, so one attachment id names both."""
    return f"{object_key_value}/thumb.webp"


def multipart_part_plan(size_bytes: int) -> MultipartPlan | None:
    """How a file of this size is split, or `None` when one request carries it."""
    settings = get_settings()
    if size_bytes < settings.attachment_multipart_threshold_bytes:
        return None
    part_size = max(MIN_PART_BYTES, settings.attachment_multipart_part_bytes)
    return MultipartPlan(part_size=part_size, part_count=-(-size_bytes // part_size))


def normalize_checksum(candidate: str | None) -> str | None:
    """Accept a lowercase SHA-256 hex digest and nothing else: the value is a lookup key."""
    value = (candidate or "").strip().lower()
    return value if CHECKSUM_PATTERN.match(value) else None


def upload_target_payload(target: UploadTarget) -> UploadTargetResponse:
    """The wire shape shared by a chat attachment target and a post media target."""
    return UploadTargetResponse(
        attachment_id=str(target.attachment_id),
        upload_url=target.upload_url,
        fields=target.fields,
        expires_in=target.expires_in,
        max_bytes=target.max_bytes,
        max_attachments=target.max_attachments,
        reused=target.reused,
        multipart=(
            MultipartPlanPayload(
                part_size=target.multipart.part_size, part_count=target.multipart.part_count
            )
            if target.multipart is not None
            else None
        ),
    )


def multipart_session_payload(session: MultipartSession) -> MultipartSessionResponse:
    return MultipartSessionResponse(
        upload_id=session.upload_id,
        part_size=session.part_size,
        expires_in=session.expires_in,
        parts=[MultipartPartPayload(**part) for part in session.parts],
    )


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
    checksum_sha256: str | None = None,
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
    ceiling = media_ceiling(declared, scope_kind=scope.kind)
    if size_bytes > ceiling:
        raise HTTPException(status_code=413, detail=_limit_detail(declared, ceiling))
    used = quota_used(database, user.id)
    if used + size_bytes > settings.attachment_account_quota_bytes:
        raise HTTPException(
            status_code=409,
            detail="Attachment storage is full for this account. Delete something to free space.",
        )

    reap_expired_uploads(database)

    checksum = normalize_checksum(checksum_sha256)
    if checksum is not None:
        reused = _copy_existing_upload(
            database,
            user=user,
            scope=scope,
            declared=declared,
            size_bytes=size_bytes,
            filename=filename,
            checksum=checksum,
        )
        if reused is not None:
            return reused

    settings_expiry = datetime.now(UTC) + timedelta(seconds=settings.attachment_upload_ttl_seconds)
    attachment_id = uuid4()
    key = object_key(scope, attachment_id)
    target = get_storage().presign_post(
        key,
        content_type=declared,
        max_bytes=ceiling,
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
        max_bytes=ceiling,
        max_attachments=max_items_for(scope.kind),
        multipart=multipart_part_plan(size_bytes),
    )


def _copy_existing_upload(
    database: Session,
    *,
    user: User,
    scope: Scope,
    declared: str,
    size_bytes: int,
    filename: str,
    checksum: str,
) -> UploadTarget | None:
    """Answer a repeat upload with a server-side copy of bytes this account already stored.

    The checksum, the declared type, and the size must all match, and the source must be this
    account's own verified, unexpired file. Only the storage service moves the bytes, so the
    client link carries nothing at all. The copy is a separate object rather than a shared
    one: each attachment is still deleted on its own terms, so release, moderation, and account
    deletion stay exactly as they were.

    A copy that storage refuses (a lost object, an unavailable service) is not an error the
    uploader caused, so the caller falls back to issuing an ordinary target.
    """
    source = database.scalar(
        select(Attachment)
        .where(
            Attachment.owner_user_id == user.id,
            Attachment.source_checksum_sha256 == checksum,
            Attachment.declared_mime_type == declared,
            Attachment.declared_size_bytes == size_bytes,
            Attachment.scan_state == SCANNED,
            Attachment.deleted_at.is_(None),
        )
        .order_by(Attachment.created_at.desc())
        .limit(1)
    )
    if source is None:
        return None

    settings = get_settings()
    attachment_id = uuid4()
    key = object_key(scope, attachment_id)
    storage = get_storage()
    try:
        storage.copy_object(source.object_key, key, content_type=declared)
        derived = None
        if source.thumbnail_key:
            derived = thumbnail_key(key)
            storage.copy_object(
                source.thumbnail_key, derived, content_type=THUMBNAIL_MIME
            )
    except StorageError:
        try:
            storage.delete(key)
        except StorageError:
            logger.warning(
                "could not clean up a partial deduplicated copy",
                extra={"event": "attachment.dedupe_cleanup_failed", "key": key},
            )
        logger.warning(
            "could not copy an already-stored attachment",
            extra={"event": "attachment.dedupe_copy_failed", "source_id": str(source.id)},
        )
        return None

    now = datetime.now(UTC)
    database.add(
        Attachment(
            id=attachment_id,
            owner_user_id=user.id,
            object_key=key,
            thumbnail_key=derived,
            original_filename=clean_filename(filename),
            declared_mime_type=declared,
            detected_mime_type=source.detected_mime_type,
            declared_size_bytes=size_bytes,
            size_bytes=source.size_bytes,
            source_checksum_sha256=checksum,
            checksum_sha256=source.checksum_sha256,
            # Identical bytes with an identical verdict: the scan result is the source's, and
            # running it twice would only cost a second read.
            scan_state=SCANNED,
            scan_detail=source.scan_detail,
            uploaded_at=now,
            finalized_at=now,
            **scope.columns(),
        )
    )
    database.flush()
    logger.info(
        "deduplicated an upload",
        extra={
            "event": "attachment.deduplicated",
            "attachment_id": str(attachment_id),
            "source_id": str(source.id),
            "bytes": size_bytes,
        },
    )
    return UploadTarget(
        attachment_id=attachment_id,
        upload_url="",
        fields={},
        expires_in=settings.attachment_upload_ttl_seconds,
        max_bytes=media_ceiling(declared, scope_kind=scope.kind),
        max_attachments=max_items_for(scope.kind),
        reused=True,
    )


def _object_keys(attachment: Attachment) -> list[str]:
    """Every object a row owns: the original and, when it exists, its preview derivative."""
    keys = [attachment.object_key]
    if attachment.thumbnail_key:
        keys.append(attachment.thumbnail_key)
    return keys


def _abort_multipart(storage, attachment: Attachment) -> None:
    """Release the parts of an upload that will never be completed."""
    if not attachment.multipart_upload_id:
        return
    try:
        storage.abort_multipart(attachment.object_key, attachment.multipart_upload_id)
    except StorageError:
        logger.warning(
            "could not abort an abandoned multipart upload",
            extra={
                "event": "attachment.multipart_abort_failed",
                "attachment_id": str(attachment.id),
            },
        )
    attachment.multipart_upload_id = None


def _reject(database: Session, attachment: Attachment, detail: str) -> None:
    """Mark an upload unusable and remove its bytes; a rejected object is never kept."""
    attachment.scan_state = REJECTED
    attachment.scan_detail = detail[:240]
    attachment.finalized_at = datetime.now(UTC)
    storage = get_storage()
    try:
        for key in _object_keys(attachment):
            storage.delete(key)
    except StorageError:
        logger.warning(
            "could not remove a rejected object",
            extra={"event": "attachment.reject_object_failed", "attachment_id": str(attachment.id)},
        )
    _abort_multipart(storage, attachment)
    attachment.thumbnail_key = None
    database.commit()


def _hash_stream(chunks: Iterable[bytes], digest) -> Iterator[bytes]:
    """Pass chunks through, hashing as they go, so a checksum costs no extra read."""
    for chunk in chunks:
        digest.update(chunk)
        yield chunk


def _stream_chunks(storage, key: str) -> Iterator[bytes]:
    """Read an object in chunks, falling back to one buffered read for a simple backend."""
    iterate = getattr(storage, "iter_chunks", None)
    if iterate is not None:
        yield from iterate(key)
        return
    yield storage.get_bytes(key)


def _scan_stream(scanner, chunks: Iterable[bytes], declared_mime: str) -> ScanVerdict:
    """Use the streaming verdict when the scanner offers one, and buffer only if it does not."""
    stream_scan = getattr(scanner, "scan_stream", None)
    if stream_scan is not None:
        return stream_scan(chunks, declared_mime=declared_mime)
    return scanner.scan(b"".join(chunks), declared_mime=declared_mime)


def finalize_upload(database: Session, *, attachment_id: UUID, user: User) -> Attachment:
    """Verify and scan an uploaded object, then promote or reject it. Safe to replay."""
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

    ceiling = media_ceiling(attachment.declared_mime_type, scope_kind=attachment.scope_type)
    if stored.size > ceiling:
        _reject(
            database,
            attachment,
            f"the upload is {stored.size} bytes, over the {ceiling} byte limit",
        )
        raise HTTPException(status_code=413, detail=_limit_detail(attachment.declared_mime_type, ceiling))

    media: MediaType = ACCEPTED_TYPES[attachment.declared_mime_type]
    scanner = get_scanner()
    checksum = hashlib.sha256()
    if media.kind == IMAGE:
        # An image is decoded, re-encoded, and written back, so it is held whole.
        payload = storage.get_bytes(attachment.object_key)
        if len(payload) != stored.size:
            _reject(database, attachment, "the object changed while it was being verified")
            raise HTTPException(status_code=422, detail="The upload could not be verified.")
        # The digest of what arrived, before re-encoding, is what a client's own digest of the
        # same file can be matched against.
        source_digest = hashlib.sha256(payload).hexdigest()
        try:
            verdict = scanner.scan(payload, declared_mime=attachment.declared_mime_type)
        except ScanUnavailable:
            raise HTTPException(
                status_code=503, detail="Uploads are being scanned; retry in a moment."
            ) from None
        if not verdict.clean:
            _reject(database, attachment, verdict.detail or "the scan rejected the upload")
            raise HTTPException(status_code=422, detail="The upload was rejected by the scanner.")
        try:
            payload = strip_image_metadata(payload, attachment.declared_mime_type)
        except UnsupportedMedia:
            _reject(database, attachment, "the image could not be normalized")
            raise HTTPException(status_code=422, detail="The image could not be processed.") from None
        storage.put_bytes(
            attachment.object_key, payload, content_type=attachment.declared_mime_type
        )
        checksum.update(payload)
        size = len(payload)
        _store_thumbnail(attachment, payload)
    else:
        # Video and audio are read once, in chunks: hashed on the way through, and streamed
        # to the scanner instead of being held in memory.
        try:
            verdict = _scan_stream(
                scanner,
                _hash_stream(_stream_chunks(storage, attachment.object_key), checksum),
                attachment.declared_mime_type,
            )
        except ScanUnavailable:
            raise HTTPException(
                status_code=503, detail="Uploads are being scanned; retry in a moment."
            ) from None
        if not verdict.clean:
            _reject(database, attachment, verdict.detail or "the scan rejected the upload")
            raise HTTPException(status_code=422, detail="The upload was rejected by the scanner.")
        size = stored.size
        # Nothing is re-encoded on this path, so the received bytes and the stored bytes have
        # the same digest.
        source_digest = checksum.hexdigest()

    attachment.detected_mime_type = attachment.declared_mime_type
    attachment.size_bytes = size
    attachment.source_checksum_sha256 = source_digest
    attachment.checksum_sha256 = checksum.hexdigest()
    attachment.scan_state = SCANNED
    attachment.scan_detail = ""
    attachment.finalized_at = datetime.now(UTC)
    database.commit()
    return attachment


def _store_thumbnail(attachment: Attachment, payload: bytes) -> None:
    """Write the preview derivative beside the original, if one is worth keeping.

    A failure here is not a failure of the upload: the reader falls back to the original, so
    the object is promoted and only the derivative is missing.
    """
    thumbnail = render_thumbnail(payload, attachment.declared_mime_type)
    if thumbnail is None:
        return
    key = thumbnail_key(attachment.object_key)
    try:
        get_storage().put_bytes(key, thumbnail, content_type=THUMBNAIL_MIME)
    except StorageError:
        logger.warning(
            "could not write an attachment thumbnail",
            extra={"event": "attachment.thumbnail_failed", "attachment_id": str(attachment.id)},
        )
        return
    attachment.thumbnail_key = key


def _require_post_media_read(database: Session, attachment: Attachment, user: User) -> None:
    """Authorize a post's media by the read rule of the post it belongs to.

    A removed post keeps its place but not its files: the row still names them, and nobody —
    not even the author — downloads them afterwards, so the reason a post is gone stays gone.
    """
    post = database.get(Post, attachment.post_id)
    if post is None or post.removed_at is not None:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    if post.community_id is not None:
        if is_blocked_between(database, user.id, post.author_id):
            raise HTTPException(status_code=404, detail="Attachment not found.")
        community = database.get(Community, post.community_id)
        if community is None:
            raise HTTPException(status_code=404, detail="Attachment not found.")
        if community.visibility == "private" and (
            community_role(database, community.id, user.id) is None
        ):
            raise HTTPException(status_code=404, detail="Attachment not found.")
        return
    if not can_read_personal_post(database, user, post):
        raise HTTPException(status_code=404, detail="Attachment not found.")


def attachment_for_download(database: Session, *, attachment_id: UUID, user: User) -> Attachment:
    """Fail closed on every condition: unknown scope, missing membership, block, or no verdict."""
    attachment = database.get(Attachment, attachment_id)
    if attachment is None or attachment.deleted_at is not None or attachment.scan_state != SCANNED:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    if attachment.post_id is not None:
        _require_post_media_read(database, attachment, user)
        return attachment
    if attachment.scope_type == CONVERSATION:
        membership = database.get(ConversationMember, (attachment.scope_id, user.id))
        if membership is None or membership.left_at is not None:
            raise HTTPException(status_code=404, detail="Attachment not found.")
        if is_blocked_between(database, user.id, attachment.owner_user_id):
            raise HTTPException(status_code=404, detail="Attachment not found.")
        return attachment
    # An upload that is still its author's draft is theirs alone; nothing else is reachable
    # by scope, so an unrecognised scope stays a 404 rather than an open door.
    if attachment.owner_user_id == user.id:
        return attachment
    raise HTTPException(status_code=404, detail="Attachment not found.")


def media_type_for(attachment: Attachment) -> MediaType:
    return ACCEPTED_TYPES[attachment.detected_mime_type or attachment.declared_mime_type]


def open_object(attachment: Attachment, *, start: int = 0, length: int | None = None):
    """Stream an object's bytes, optionally one range of them.

    Seeking in a video is a range read: the browser asks for the part it needs, and the
    response is that part rather than the file from the beginning.
    """
    storage = get_storage()
    iterate = getattr(storage, "iter_chunks", None)
    if iterate is not None:
        return iterate(attachment.object_key, start=start, length=length)
    payload = storage.get_bytes(attachment.object_key)
    end = None if length is None else start + length
    return iter([payload[start:end]])


def open_thumbnail(attachment: Attachment):
    """Stream the preview derivative. It is small, so it is never a range read."""
    if not attachment.thumbnail_key:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    storage = get_storage()
    iterate = getattr(storage, "iter_chunks", None)
    if iterate is not None:
        return iterate(attachment.thumbnail_key)
    return iter([storage.get_bytes(attachment.thumbnail_key)])


def _pending_upload(database: Session, *, attachment_id: UUID, user: User) -> Attachment:
    """The caller's own unfinished upload, or the same non-revealing 404 as everything else."""
    attachment = database.get(Attachment, attachment_id)
    if (
        attachment is None
        or attachment.owner_user_id != user.id
        or attachment.scan_state != PENDING_UPLOAD
        or attachment.deleted_at is not None
    ):
        raise HTTPException(status_code=404, detail="Upload not found.")
    if attachment.expires_at is not None:
        expires = attachment.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires < datetime.now(UTC):
            raise HTTPException(status_code=409, detail="The upload target expired. Retry.")
    return attachment


def start_multipart(
    database: Session, *, attachment_id: UUID, user: User
) -> MultipartSession:
    """Open, or resume, the part session for one of the caller's pending uploads.

    Resuming matters more than starting: a client that lost its connection asks again and
    receives the same upload id, so the parts it already delivered are still accounted for.
    """
    settings = get_settings()
    attachment = _pending_upload(database, attachment_id=attachment_id, user=user)
    size = attachment.declared_size_bytes
    if size <= 0:
        raise HTTPException(status_code=422, detail="The upload must not be empty.")
    plan = multipart_part_plan(size) or MultipartPlan(
        part_size=max(MIN_PART_BYTES, settings.attachment_multipart_part_bytes),
        part_count=1,
    )
    storage = get_storage()
    upload_id = attachment.multipart_upload_id
    if not upload_id:
        try:
            upload_id = storage.create_multipart(
                attachment.object_key, content_type=attachment.declared_mime_type
            )
        except StorageError:
            raise HTTPException(
                status_code=503, detail="Object storage is unavailable. Retry in a moment."
            ) from None
        attachment.multipart_upload_id = upload_id
        # The part URLs are issued now, so the recorded deadline must match them: a target
        # issued earlier must not be reaped while its parts are still arriving.
        attachment.expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.attachment_upload_ttl_seconds
        )
        database.commit()
    parts: list[dict[str, int | str]] = []
    for number in range(1, plan.part_count + 1):
        offset = (number - 1) * plan.part_size
        try:
            url = storage.presign_part(
                attachment.object_key,
                upload_id,
                number,
                expires_in=settings.attachment_upload_ttl_seconds,
            )
        except StorageError:
            raise HTTPException(
                status_code=503, detail="Object storage is unavailable. Retry in a moment."
            ) from None
        parts.append(
            {
                "number": number,
                "offset": offset,
                "size": min(plan.part_size, size - offset),
                "url": url,
            }
        )
    return MultipartSession(
        upload_id=upload_id,
        part_size=plan.part_size,
        parts=parts,
        expires_in=settings.attachment_upload_ttl_seconds,
    )


def complete_multipart(
    database: Session,
    *,
    attachment_id: UUID,
    user: User,
    parts: Iterable[tuple[int, str]],
) -> Attachment:
    """Assemble the parts, then run the same verify-and-scan step a single POST would."""
    attachment = database.get(Attachment, attachment_id)
    if (
        attachment is None
        or attachment.owner_user_id != user.id
        or attachment.deleted_at is not None
        or attachment.scan_state == REJECTED
    ):
        raise HTTPException(status_code=404, detail="Upload not found.")
    # A replay after the parts were assembled (a lost response, or a scanner that was down)
    # owes only the verification step, which is itself safe to replay.
    if attachment.multipart_upload_id is None:
        if attachment.scan_state in (PENDING_SCAN, SCANNED):
            return finalize_upload(database, attachment_id=attachment_id, user=user)
        raise HTTPException(status_code=409, detail="No upload is in progress for this file.")
    if attachment.scan_state != PENDING_UPLOAD:
        raise HTTPException(status_code=409, detail="No upload is in progress for this file.")
    upload_id = attachment.multipart_upload_id
    ordered = sorted(parts, key=lambda part: part[0])
    numbers = [number for number, _etag in ordered]
    expected = (multipart_part_plan(attachment.declared_size_bytes) or MultipartPlan(1, 1)).part_count
    if numbers != list(range(1, expected + 1)):
        raise HTTPException(status_code=422, detail="The uploaded parts are incomplete.")
    for _number, etag in ordered:
        if not etag or len(etag) > 200:
            raise HTTPException(status_code=422, detail="A part is missing its ETag.")
    try:
        get_storage().complete_multipart(
            attachment.object_key,
            upload_id,
            [{"PartNumber": number, "ETag": etag.strip()} for number, etag in ordered],
        )
    except StorageError:
        raise HTTPException(
            status_code=503, detail="The upload could not be completed. Retry in a moment."
        ) from None
    attachment.multipart_upload_id = None
    database.commit()
    return finalize_upload(database, attachment_id=attachment_id, user=user)


def parse_byte_range(header: str, size: int) -> tuple[int, int] | None:
    """Read one byte range out of a `Range` header.

    Returns `(start, end)` inclusive, `None` when the header is absent or asks for something
    this route does not do (several ranges, or a unit other than bytes), and raises for a
    range the object cannot satisfy so the caller can answer 416.
    """
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes=") :].split(",")[0].strip()
    first, _, last = spec.partition("-")
    try:
        if not first:
            # A suffix range asks for the final N bytes, which is how a player finds a tail.
            suffix = int(last)
            if suffix <= 0:
                return None
            return max(size - suffix, 0), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or end < start:
        raise ValueError("unsatisfiable")
    return start, min(end, size - 1)


def record_delivery(database: Session, *, attachment: Attachment, user: User) -> bool:
    """Note that `user` now holds the bytes, and drop the shared copy once everyone does.

    The owner's own download is not a delivery: there is one stored copy, and the point is to
    release it once the people it was sent to have it. Returns True when this call removed the
    stored object.

    A post's media is not released this way. A chat file exists to be received by the people
    it was addressed to; a post is read by whoever arrives later, so its bytes stay until the
    account removes them.
    """
    if attachment.owner_user_id == user.id or attachment.deleted_at is not None:
        return False
    if attachment.scope_type != CONVERSATION:
        return False
    if database.get(AttachmentDelivery, (attachment.id, user.id)) is None:
        database.add(AttachmentDelivery(attachment_id=attachment.id, user_id=user.id))
        database.flush()

    recipients = set(
        database.scalars(
            select(ConversationMember.user_id).where(
                ConversationMember.conversation_id == attachment.scope_id,
                ConversationMember.left_at.is_(None),
                ConversationMember.user_id != attachment.owner_user_id,
            )
        ).all()
    )
    delivered = set(
        database.scalars(
            select(AttachmentDelivery.user_id).where(
                AttachmentDelivery.attachment_id == attachment.id
            )
        ).all()
    )
    if recipients and recipients <= delivered:
        expire_attachment(database, attachment)
        return True
    database.commit()
    return False


def expire_attachment(database: Session, attachment: Attachment) -> None:
    """Delete the stored object and leave the row as a tombstone the transcript can name."""
    storage = get_storage()
    try:
        for key in _object_keys(attachment):
            storage.delete(key)
    except StorageError:
        logger.warning(
            "could not remove a delivered attachment's object",
            extra={
                "event": "attachment.expire_object_failed",
                "attachment_id": str(attachment.id),
            },
        )
    attachment.thumbnail_key = None
    attachment.deleted_at = datetime.now(UTC)
    database.commit()


def attachment_payload(attachment: Attachment) -> AttachmentPayload:
    """The client-facing shape: verified metadata plus routes, never a storage URL."""
    media = media_type_for(attachment)
    return AttachmentPayload(
        id=str(attachment.id),
        filename=attachment.original_filename,
        content_type=media.mime,
        size_bytes=attachment.size_bytes,
        inline=media.inline,
        kind=media.kind,
        expired=attachment.deleted_at is not None,
        download_url=f"/attachments/{attachment.id}/download",
        # A preview is a rendering hint, not the file: the reader still downloads the original
        # to keep a copy, which is what releases the stored object.
        thumbnail_url=(
            f"/attachments/{attachment.id}/thumbnail"
            if attachment.thumbnail_key and attachment.deleted_at is None
            else None
        ),
    )


def attachments_for_message(database: Session, message_id: UUID) -> list[Attachment]:
    return list(
        database.scalars(
            select(Attachment)
            .where(Attachment.message_id == message_id, Attachment.scan_state == SCANNED)
            .order_by(Attachment.created_at)
        )
    )


def attachments_for_post(database: Session, post_id: UUID) -> list[Attachment]:
    return list(
        database.scalars(
            select(Attachment)
            .where(
                Attachment.post_id == post_id,
                Attachment.scan_state == SCANNED,
                Attachment.deleted_at.is_(None),
            )
            .order_by(Attachment.created_at)
        )
    )


def post_media_payloads(
    database: Session, post_ids: Iterable[UUID]
) -> dict[UUID, list[AttachmentPayload]]:
    """Verified media for a page of posts, in one query rather than one per post."""
    wanted = list(post_ids)
    if not wanted:
        return {}
    rows = database.scalars(
        select(Attachment)
        .where(
            Attachment.post_id.in_(wanted),
            Attachment.scan_state == SCANNED,
            Attachment.deleted_at.is_(None),
        )
        .order_by(Attachment.created_at)
    ).all()
    grouped: dict[UUID, list[AttachmentPayload]] = {}
    for attachment in rows:
        grouped.setdefault(attachment.post_id, []).append(attachment_payload(attachment))
    return grouped


def can_attach_count(attachment_ids: Iterable[UUID]) -> bool:
    """Whether this many files could join one post, checked before the post is written."""
    ordered = list(attachment_ids)
    return (
        len(ordered) <= get_settings().post_media_limit
        and len(set(ordered)) == len(ordered)
    )


def attach_to_post(
    database: Session, *, post: Post, user: User, attachment_ids: Iterable[UUID]
) -> list[Attachment]:
    """Bind finalized uploads to a post. Only verified, unclaimed, own uploads qualify.

    The scope is checked as well as the owner: an upload issued for a conversation cannot be
    walked into a post, and a post cannot borrow its author's draft for somebody else.
    """
    settings = get_settings()
    ordered = list(attachment_ids)
    if len(ordered) > settings.post_media_limit:
        raise HTTPException(
            status_code=422, detail=f"A post can carry {settings.post_media_limit} files."
        )
    if len(set(ordered)) != len(ordered):
        raise HTTPException(status_code=422, detail="A file was listed twice.")
    attachments: list[Attachment] = []
    for attachment_id in ordered:
        attachment = database.get(Attachment, attachment_id)
        if (
            attachment is None
            or attachment.owner_user_id != user.id
            or attachment.scan_state != SCANNED
            or attachment.post_id is not None
            or attachment.deleted_at is not None
            or attachment.scope_type != ACCOUNT
            or attachment.scope_id != user.id
        ):
            raise HTTPException(
                status_code=422, detail="One of these files cannot join this post."
            )
        attachments.append(attachment)
    for attachment in attachments:
        attachment.post_id = post.id
    return attachments


def discard_draft(database: Session, *, attachment_id: UUID, user: User) -> bool:
    """Drop an upload the composer no longer wants, bytes first, then the row.

    Only an unclaimed upload of the caller's may be discarded: once it belongs to a post, the
    post decides what happens to it.
    """
    attachment = database.get(Attachment, attachment_id)
    if (
        attachment is None
        or attachment.owner_user_id != user.id
        or attachment.post_id is not None
        or attachment.message_id is not None
    ):
        return False
    delete_attachments(database, [attachment])
    database.commit()
    return True


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
    storage = get_storage()
    keys = [key for attachment in attachments for key in _object_keys(attachment)]
    try:
        storage.delete_many(keys)
    except StorageError:
        # Revoking access matters more than reclaiming the bytes, so the rows still go and
        # the failure is logged for an operator to reconcile.
        logger.warning(
            "could not remove attachment objects",
            extra={"event": "attachment.delete_object_failed", "objects": len(keys)},
        )
    for attachment in attachments:
        _abort_multipart(storage, attachment)
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
