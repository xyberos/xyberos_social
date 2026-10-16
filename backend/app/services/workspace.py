"""The workspace: everything the reader has published, in one bounded list.

A personal update and a community post are rows in the same table, so the reader manages both
from here. The list is a keyset window rather than an unbounded dump, and a post that lives in
a community the reader has left stays visible but stops being a link, because the community
page would answer 404 to a non-member.
"""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Community, CommunityMembership, Post

PAGE_SIZE = 50
SOURCES = ("all", "personal", "communities")


def _conditions(author_id: UUID, source: str) -> list:
    conditions = [Post.author_id == author_id]
    if source == "personal":
        conditions.append(Post.community_id.is_(None))
    elif source == "communities":
        conditions.append(Post.community_id.is_not(None))
    return conditions


def count_posts(database: Session, author_id: UUID, source: str) -> int:
    return (
        database.scalar(
            select(func.count()).select_from(Post).where(*_conditions(author_id, source))
        )
        or 0
    )


def encode_cursor(post: Post) -> str:
    payload = f"{post.created_at.isoformat()}|{post.id}"
    return urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        payload = urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
        created_at, post_id = payload.split("|")
        return datetime.fromisoformat(created_at), UUID(post_id)
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=422, detail="Invalid workspace cursor.") from error


def list_posts(
    database: Session,
    author_id: UUID,
    source: str,
    after: tuple[datetime, UUID] | None,
    limit: int,
) -> tuple[list[tuple[Post, Community | None, bool]], str | None]:
    """One page of (post, community, openable) rows, newest first, with the next cursor."""
    member_of = select(CommunityMembership.community_id).where(
        CommunityMembership.user_id == author_id
    )
    conditions = _conditions(author_id, source)
    if after is not None:
        created_at, post_id = after
        if database.bind.dialect.name == "sqlite":
            post_time = func.strftime("%Y-%m-%d %H:%M:%f", Post.created_at)
            cursor_time = func.strftime("%Y-%m-%d %H:%M:%f", created_at)
        else:
            post_time = Post.created_at
            cursor_time = created_at
        conditions.append(
            or_(post_time < cursor_time, (post_time == cursor_time) & (Post.id < post_id))
        )
    rows = database.execute(
        select(Post, Community, Community.id.in_(member_of))
        .outerjoin(Community, Community.id == Post.community_id)
        .where(*conditions)
        .order_by(Post.created_at.desc(), Post.id.desc())
        .limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = encode_cursor(visible[-1][0]) if has_more and visible else None
    return (
        [
            (post, community, community is None or community.visibility == "public" or bool(member))
            for post, community, member in visible
        ],
        next_cursor,
    )
