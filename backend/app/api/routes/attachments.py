"""Attachment upload targets, finalize, and authorized downloads.

Three steps, and each one enforces its own rule: issuing a target authorizes the uploader
and applies the type, size, and quota ceilings; finalize verifies what actually arrived; and
download re-authorizes the reader against the conversation the attachment belongs to.
"""

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import authenticated_user
from app.core.database import get_db
from app.core.scoping import conversation_scope
from app.core.security import check_rate_limit
from app.core.storage import StorageError, get_storage
from app.models import User
from app.schemas.attachments import UploadTargetRequest, UploadTargetResponse
from app.services import attachments as attachment_service
from app.services import idempotency
from app.services.messaging import active_conversation_member

router = APIRouter()

UPLOAD_LIMIT = 20
DOWNLOAD_LIMIT = 240
RATE_WINDOW_SECONDS = 60


def _signed_in(request: Request, database: Session) -> User:
    user = authenticated_user(request, database)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


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
    )
    body = UploadTargetResponse(
        attachment_id=str(target.attachment_id),
        upload_url=target.upload_url,
        fields=target.fields,
        expires_in=target.expires_in,
        max_bytes=target.max_bytes,
        max_attachments=target.max_attachments,
    ).model_dump()
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


@router.get("/attachments/{attachment_id}/download")
def download_attachment(
    attachment_id: UUID,
    request: Request,
    database: Session = Depends(get_db),
):
    """Serve bytes to a reader who is entitled to them right now, never from a public URL."""
    user = _signed_in(request, database)
    if not check_rate_limit(
        f"attachment:{user.id}", "download", limit=DOWNLOAD_LIMIT, seconds=RATE_WINDOW_SECONDS
    ):
        raise HTTPException(status_code=429, detail="Too many downloads. Try again in a moment.")
    attachment = attachment_service.attachment_for_download(
        database, attachment_id=attachment_id, user=user
    )
    media = attachment_service.media_type_for(attachment)
    try:
        body = get_storage().get_bytes(attachment.object_key)
    except StorageError:
        raise HTTPException(
            status_code=503, detail="The attachment is temporarily unavailable."
        ) from None

    headers = {
        # Every download states what it is; only allowlisted images may be rendered inline,
        # and the filename is encoded so it cannot inject a header of its own.
        "Content-Disposition": (
            f"{'inline' if media.inline else 'attachment'}; "
            f"filename*=UTF-8''{quote(attachment.original_filename)}"
        ),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=300",
    }
    if media.inline:
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return Response(content=body, media_type=media.mime, headers=headers)
