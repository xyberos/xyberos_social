"""JSON contract for the bounded notifications API."""

from datetime import datetime

from pydantic import BaseModel


class NotificationItem(BaseModel):
    id: str
    category: str
    message: str
    target_url: str | None
    created_at: datetime
    read_at: datetime | None


class NotificationPage(BaseModel):
    notifications: list[NotificationItem]
    count: int
    has_more: bool
    next_cursor: str | None
