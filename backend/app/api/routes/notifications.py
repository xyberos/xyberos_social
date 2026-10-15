"""Bounded notification inbox and its JSON API."""

from datetime import UTC, datetime
from uuid import UUID
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.api.deps import authenticated_form_user, authenticated_user, current_session
from app.api.responses import render
from app.core.database import get_db
from app.models import Notification
from app.schemas.notifications import NotificationItem, NotificationPage
from app.services.notifications import notification_batch

router = APIRouter()


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(
    request: Request,
    limit: int = 20,
    cursor: str | None = None,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=422, detail="Notification pages are limited to 50 items.")
    rows, has_more, next_cursor = notification_batch(database, user, limit, cursor)
    unread_count = database.scalar(
        select(func.count(Notification.id)).where(
            Notification.recipient_user_id == user.id,
            Notification.read_at.is_(None),
        )
    )
    browser_session = current_session(request, database)
    return render(
        request,
        "notifications.html",
        {
            "user": user,
            "notifications": rows,
            "count": len(rows),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "unread_count": unread_count,
            "limit": limit,
            "csrf_token": browser_session.csrf_token,
        },
    )


@router.get("/api/v1/notifications", response_model=NotificationPage)
def notifications_api(
    request: Request,
    limit: int = 20,
    cursor: str | None = None,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return JSONResponse({"detail": "Authentication required."}, status_code=401)
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=422, detail="Notification pages are limited to 50 items.")
    rows, has_more, next_cursor = notification_batch(database, user, limit, cursor)
    return NotificationPage(
        notifications=[
            NotificationItem(
                id=str(notification.id),
                category=notification.category,
                message=notification.message,
                target_url=notification.target_url,
                created_at=notification.created_at,
                read_at=notification.read_at,
            )
            for notification, _actor in rows
        ],
        count=len(rows),
        has_more=has_more,
        next_cursor=next_cursor,
    )


@router.post("/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    notification = database.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.recipient_user_id == user.id,
        )
    )
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found.")
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        database.commit()
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/read-all")
def mark_all_notifications_read(
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    database.query(Notification).filter(
        Notification.recipient_user_id == user.id,
        Notification.read_at.is_(None),
    ).update({Notification.read_at: datetime.now(UTC)}, synchronize_session=False)
    database.commit()
    return RedirectResponse("/notifications", status_code=303)


@router.post("/notifications/clear")
def clear_notifications(
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    database.query(Notification).filter(Notification.recipient_user_id == user.id).delete(
        synchronize_session=False
    )
    database.commit()
    return RedirectResponse("/notifications", status_code=303)
