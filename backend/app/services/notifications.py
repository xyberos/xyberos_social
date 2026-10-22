"""Preference-aware notification creation and bounded cursor pagination."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from app.models import Notification, User


def queue_notification(
    database: Session,
    recipient: User,
    actor: User,
    category: str,
    message: str,
    target_url: str | None = None,
) -> None:
    if (
        recipient.id == actor.id
        or not recipient.is_active
        or recipient.quiet_mode
        or (category == "reply" and not recipient.notify_replies)
        or (category == "moderation" and not recipient.notify_moderation)
        or (category == "call" and not recipient.notify_calls)
    ):
        return
    database.add(
        Notification(
            recipient_user_id=recipient.id,
            actor_user_id=actor.id,
            category=category,
            message=message[:240],
            target_url=target_url,
        )
    )


def encode_notification_cursor(notification: Notification) -> str:
    payload = f"{notification.created_at.isoformat()}|{notification.id}"
    return urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_notification_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        payload = urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
        created_at, notification_id = payload.split("|")
        return datetime.fromisoformat(created_at), UUID(notification_id)
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=422, detail="Invalid notification cursor.") from error


def notification_batch(
    database: Session,
    user: User,
    limit: int,
    cursor: str | None,
) -> tuple[list[tuple[Notification, User | None]], bool, str | None]:
    statement = (
        select(Notification, User)
        .outerjoin(User, User.id == Notification.actor_user_id)
        .where(Notification.recipient_user_id == user.id)
    )
    if cursor:
        cursor_created_at, cursor_id = decode_notification_cursor(cursor)
        if database.bind.dialect.name == "sqlite":
            notification_time = func.strftime("%Y-%m-%d %H:%M:%f", Notification.created_at)
            cursor_time = func.strftime("%Y-%m-%d %H:%M:%f", cursor_created_at)
        else:
            notification_time = Notification.created_at
            cursor_time = cursor_created_at
        statement = statement.where(
            or_(
                notification_time < cursor_time,
                (notification_time == cursor_time) & (Notification.id < cursor_id),
            )
        )
    rows = database.execute(
        statement.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = encode_notification_cursor(page_rows[-1][0]) if has_more else None
    return page_rows, has_more, next_cursor
