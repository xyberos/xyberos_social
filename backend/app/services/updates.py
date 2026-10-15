"""Stable keyset cursors for the finite updates window."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime
from uuid import UUID
from fastapi import HTTPException
from app.models import Post


def encode_updates_cursor(post: Post, window_end: datetime) -> str:
    payload = f"{post.created_at.isoformat()}|{post.id}|{window_end.isoformat()}"
    return urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_updates_cursor(cursor: str) -> tuple[datetime, UUID, datetime]:
    try:
        payload = urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
        created_at, post_id, window_end = payload.split("|")
        return datetime.fromisoformat(created_at), UUID(post_id), datetime.fromisoformat(window_end)
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=422, detail="Invalid updates cursor.") from error
