"""Upload targets for a post's photos, video, and audio, and withdrawal of a draft.

A post's media follows the same three steps as a chat attachment — issue a target, let the
browser put the bytes in storage, then finalize and scan — but the scope is the author's
account rather than a conversation, because the post that will own the file does not exist
yet. Binding happens when the post is created, and `POST /posts` is what refuses an upload
that is not verified, not the author's, or already spoken for.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import authenticated_user
from app.core.database import get_db
from app.core.scoping import account_scope
from app.core.security import check_rate_limit
from app.models import User
from app.schemas.attachments import UploadTargetRequest, UploadTargetResponse
from app.services import attachments as attachment_service
from app.services import idempotency

router = APIRouter()

UPLOAD_LIMIT = 20
RATE_WINDOW_SECONDS = 60


def _signed_in(request: Request, database: Session) -> User:
    user = authenticated_user(request, database)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


@router.post("/posts/media", status_code=201)
def create_post_media_target(
    payload: UploadTargetRequest,
    request: Request,
    database: Session = Depends(get_db),
):
    """Issue a short-lived upload target for one file a post will carry."""
    user = _signed_in(request, database)
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
            endpoint="create_post_media_target",
            fingerprint=idempotency.request_fingerprint(
                payload.content_type.strip().lower(),
                payload.size_bytes,
                payload.filename,
            ),
        )
        if reservation.replay is not None and reservation.replay.body is not None:
            return JSONResponse(
                reservation.replay.body, status_code=reservation.replay.status_code
            )

    target = attachment_service.issue_upload_target(
        database,
        user=user,
        # The post does not exist yet, so the upload belongs to the account until the post
        # claims it. A draft is readable by its author alone.
        scope=account_scope(user.id),
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


@router.delete("/posts/media/{attachment_id}", status_code=204)
def discard_post_media(
    attachment_id: UUID,
    request: Request,
    database: Session = Depends(get_db),
):
    """Withdraw a file the composer attached and then removed, releasing its bytes now.

    A `DELETE` rather than a form post: a cross-site form cannot send one, and the session
    cookie is SameSite=Lax, so the request cannot be made on the reader's behalf.
    """
    user = _signed_in(request, database)
    if not attachment_service.discard_draft(database, attachment_id=attachment_id, user=user):
        # A file already claimed by a post is the post's business, and an id the caller does
        # not own is indistinguishable from one that never existed.
        raise HTTPException(status_code=404, detail="Upload not found.")
    return JSONResponse(None, status_code=204)
