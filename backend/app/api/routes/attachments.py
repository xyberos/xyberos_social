"""Attachment upload targets, finalize, delivery confirmation, and authorized downloads.

Four steps, and each one enforces its own rule: issuing a target authorizes the uploader and
applies the type, size, and quota ceilings; finalize verifies what actually arrived; download
re-authorizes the reader against the conversation or post the attachment belongs to; and the
reader's confirmation releases the single stored copy of a chat file once everyone it was
sent to has one.
"""

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import authenticated_form_user, authenticated_user
from app.core.database import get_db
from app.core.media import THUMBNAIL_MIME
from app.core.scoping import conversation_scope
from app.core.security import check_rate_limit
from app.core.storage import StorageError
from app.models import User
from app.schemas.attachments import (
    MultipartCompleteRequest,
    UploadTargetRequest,
)
from app.services import attachments as attachment_service
from app.services import idempotency
from app.services.messaging import active_conversation_member

router = APIRouter()

UPLOAD_LIMIT = 20
DOWNLOAD_LIMIT = 240
RATE_WINDOW_SECONDS = 60

# Attachment bytes never change after finalize: the object key is per attachment and the
# content is verified against a checksum. A long private lifetime lets a reader's browser (and
# the local attachment cache) answer a repeat view without a single byte leaving storage.
ATTACHMENT_CACHE_CONTROL = "private, max-age=86400, immutable"


def _signed_in(request: Request, database: Session) -> User:
    user = authenticated_user(request, database)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def _etag_matches(header: str, etag: str) -> bool:
    """Compare an `If-None-Match` header, which may list several tags or use the weak form."""
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == "*" or candidate == etag:
            return True
    return False


@router.post("/conversations/{conversation_id}/attachments", status_code=201)
def create_attachment_target(
    conversation_id: UUID,
    payload: UploadTargetRequest,
    request: Request,
    database: Session = Depends(get_db),
):
    """Issue a short-lived upload target for one declared file."""
    user = _signed_in(request, database)
    # Participation is checked here as well as at finalize: an upload target is a capability,
    # and it is only ever handed to someone who belongs in the conversation.
    active_conversation_member(database, conversation_id, user.id)
    if not check_rate_limit(
        f"attachment:{user.id}", "upload", limit=UPLOAD_LIMIT, seconds=RATE_WINDOW_SECONDS
    ):
        raise HTTPException(status_code=429, detail="Too many uploads. Try again in a moment.")

    key = idempotency.presented_key(request)
    reservation = None
    if key is not None:
        reservation = idempotency.begin(
            database,
            scope=idempotency.actor_scope(user.id),
            key=key,
            endpoint="create_attachment_target",
            fingerprint=idempotency.request_fingerprint(
                conversation_id,
                payload.content_type.strip().lower(),
                payload.size_bytes,
                payload.filename,
                payload.checksum_sha256 or "",
            ),
        )
        if reservation.replay is not None and reservation.replay.body is not None:
            # Replaying the same target is correct: the key, the scope, and the ceiling match.
            return JSONResponse(
                reservation.replay.body, status_code=reservation.replay.status_code
            )

    target = attachment_service.issue_upload_target(
        database,
        user=user,
        scope=conversation_scope(conversation_id),
        declared_mime_type=payload.content_type,
        size_bytes=payload.size_bytes,
        filename=payload.filename,
        checksum_sha256=payload.checksum_sha256,
    )
    body = attachment_service.upload_target_payload(target).model_dump()
    if reservation is not None:
        idempotency.complete(database, reservation, status_code=201, body=body)
    database.commit()
    return JSONResponse(body, status_code=201)


@router.post("/attachments/{attachment_id}/finalize")
def finalize_attachment(
    attachment_id: UUID,
    request: Request,
    database: Session = Depends(get_db),
):
    """Verify, scan, and promote an upload. Replaying a finalized upload is a no-op."""
    user = _signed_in(request, database)
    attachment = attachment_service.finalize_upload(
        database, attachment_id=attachment_id, user=user
    )
    return attachment_service.attachment_payload(attachment)


@router.post("/attachments/{attachment_id}/received")
def confirm_attachment_delivery(
    attachment_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    """Confirm the reader's browser kept a copy, which is what releases the stored object.

    The download alone is not the trigger: a reader that fetched the bytes but could not keep
    them (storage disabled, private window) would otherwise lose the file for good. Only the
    page that actually stored a local copy says so.
    """
    user, _ = authenticated_form_user(request, database, csrf_token)
    attachment = attachment_service.attachment_for_download(
        database, attachment_id=attachment_id, user=user
    )
    released = attachment_service.record_delivery(database, attachment=attachment, user=user)
    return {"status": "ok", "released": released, "expired": attachment.deleted_at is not None}


@router.get("/attachments/{attachment_id}/download")
def download_attachment(
    attachment_id: UUID,
    request: Request,
    database: Session = Depends(get_db),
):
    """Serve bytes to a reader who is entitled to them right now, never from a public URL.

    The response is streamed and range-aware, because a video is played by a browser that
    asks for one part of it at a time. Ranges are answered from the size this pipeline
    recorded at finalize, so a reader cannot ask for bytes past what was verified.
    """
    user = _signed_in(request, database)
    if not check_rate_limit(
        f"attachment:{user.id}", "download", limit=DOWNLOAD_LIMIT, seconds=RATE_WINDOW_SECONDS
    ):
        raise HTTPException(status_code=429, detail="Too many downloads. Try again in a moment.")
    attachment = attachment_service.attachment_for_download(
        database, attachment_id=attachment_id, user=user
    )
    media = attachment_service.media_type_for(attachment)
    size = attachment.size_bytes

    headers = {
        # Every download states what it is; only allowlisted media may be rendered inline,
        # and the filename is encoded so it cannot inject a header of its own.
        "Content-Disposition": (
            f"{'inline' if media.inline else 'attachment'}; "
            f"filename*=UTF-8''{quote(attachment.original_filename)}"
        ),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": ATTACHMENT_CACHE_CONTROL,
        "Accept-Ranges": "bytes",
    }
    etag = f'"{attachment.checksum_sha256}"' if attachment.checksum_sha256 else None
    if etag:
        headers["ETag"] = etag
        # Revalidation is how a cached copy is proven current without resending the bytes.
        if _etag_matches(request.headers.get("if-none-match", ""), etag):
            return Response(status_code=304, headers=headers)
    if media.inline:
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"

    try:
        requested = attachment_service.parse_byte_range(request.headers.get("range", ""), size)
    except ValueError:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    try:
        if requested is None:
            stream = attachment_service.open_object(attachment)
            headers["Content-Length"] = str(size)
            return StreamingResponse(stream, media_type=media.mime, headers=headers)
        start, end = requested
        length = end - start + 1
        stream = attachment_service.open_object(attachment, start=start, length=length)
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(length)
        return StreamingResponse(stream, status_code=206, media_type=media.mime, headers=headers)
    except StorageError:
        raise HTTPException(
            status_code=503, detail="The attachment is temporarily unavailable."
        ) from None


@router.get("/attachments/{attachment_id}/thumbnail")
def download_thumbnail(
    attachment_id: UUID,
    request: Request,
    database: Session = Depends(get_db),
):
    """Serve the small preview derivative under the same authorization as the original.

    A preview is what a bubble or a feed tile renders, so this route is the one that keeps a
    page of media from costing a page of originals. The original is still what a reader
    downloads, and still what releases the stored copy.
    """
    user = _signed_in(request, database)
    if not check_rate_limit(
        f"attachment:{user.id}", "download", limit=DOWNLOAD_LIMIT, seconds=RATE_WINDOW_SECONDS
    ):
        raise HTTPException(status_code=429, detail="Too many downloads. Try again in a moment.")
    attachment = attachment_service.attachment_for_download(
        database, attachment_id=attachment_id, user=user
    )
    if not attachment.thumbnail_key:
        raise HTTPException(status_code=404, detail="Attachment not found.")

    headers = {
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": ATTACHMENT_CACHE_CONTROL,
    }
    if attachment.checksum_sha256:
        etag = f'"{attachment.checksum_sha256}-preview"'
        headers["ETag"] = etag
        if _etag_matches(request.headers.get("if-none-match", ""), etag):
            return Response(status_code=304, headers=headers)

    try:
        stream = attachment_service.open_thumbnail(attachment)
    except StorageError:
        raise HTTPException(
            status_code=503, detail="The attachment is temporarily unavailable."
        ) from None
    return StreamingResponse(stream, media_type=THUMBNAIL_MIME, headers=headers)


@router.post("/attachments/{attachment_id}/multipart")
def start_attachment_multipart(
    attachment_id: UUID,
    request: Request,
    database: Session = Depends(get_db),
):
    """Open or resume the part session for a large upload.

    Asking twice returns the same upload id, so a client that lost its connection can carry
    on with the parts it already delivered instead of sending the whole file again.
    """
    user = _signed_in(request, database)
    session = attachment_service.start_multipart(
        database, attachment_id=attachment_id, user=user
    )
    return attachment_service.multipart_session_payload(session)


@router.post("/attachments/{attachment_id}/multipart/complete")
def complete_attachment_multipart(
    attachment_id: UUID,
    payload: MultipartCompleteRequest,
    request: Request,
    database: Session = Depends(get_db),
):
    """Assemble the uploaded parts, then verify and scan the result."""
    user = _signed_in(request, database)
    attachment = attachment_service.complete_multipart(
        database,
        attachment_id=attachment_id,
        user=user,
        parts=[(part.number, part.etag) for part in payload.parts],
    )
    return attachment_service.attachment_payload(attachment)
