"""Signed-out landing page and the finite updates window."""

from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from app.api.deps import authenticated_user
from app.api.responses import render
from app.core.database import get_db
from app.models import Community, CommunityMembership, Post, User, UserRelationship
from app.services.updates import decode_updates_cursor, encode_updates_cursor

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request, database: Session = Depends(get_db)):
    user = authenticated_user(request, database)
    if user is not None:
        return RedirectResponse("/conversations", status_code=303)
    return render(request, "home.html", {"user": user})


@router.get("/updates", response_class=HTMLResponse)
def updates_page(
    request: Request,
    source: str | None = None,
    days: int | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    source = user.updates_source if source is None else source
    days = user.updates_days if days is None else days
    limit = user.updates_limit if limit is None else limit
    if source not in {"all", "people", "communities"}:
        raise HTTPException(status_code=422, detail="Choose a valid update source.")
    if days not in {1, 7, 30, 90}:
        raise HTTPException(status_code=422, detail="Choose a valid time range.")
    if limit not in {1, 5, 10, 20, 50}:
        raise HTTPException(status_code=422, detail="Choose a valid batch size.")

    if cursor:
        cursor_created_at, cursor_post_id, window_end = decode_updates_cursor(cursor)
    else:
        cursor_created_at = cursor_post_id = None
        window_end = datetime.now(UTC)
    window_start = window_end - timedelta(days=days)

    followed_authors = select(UserRelationship.target_user_id).where(
        UserRelationship.source_user_id == user.id,
        UserRelationship.kind == "follow",
    )
    joined_communities = select(CommunityMembership.community_id).where(
        CommunityMembership.user_id == user.id
    )
    source_conditions = []
    visibility_conditions = [
        Community.visibility == "public",
        Community.id.in_(joined_communities),
    ]
    if source in {"all", "people"}:
        source_conditions.append(Post.author_id.in_(followed_authors))
        # A personal update has no community to gate on: the author's followers are its
        # audience, and this source is exactly the list of people the reader follows.
        visibility_conditions.append(Post.community_id.is_(None))
    if source in {"all", "communities"}:
        source_conditions.append(Post.community_id.in_(joined_communities))

    blocked_authors = (
        select(UserRelationship.target_user_id)
        .where(
            UserRelationship.source_user_id == user.id,
            UserRelationship.kind == "block",
        )
        .union(
            select(UserRelationship.source_user_id).where(
                UserRelationship.target_user_id == user.id,
                UserRelationship.kind == "block",
            )
        )
    )
    conditions = [
        Post.removed_at.is_(None),
        Post.created_at >= window_start,
        Post.created_at <= window_end,
        User.is_active.is_(True),
        Post.author_id.not_in(blocked_authors),
        or_(*visibility_conditions),
        or_(*source_conditions),
    ]
    if cursor_created_at is not None and cursor_post_id is not None:
        if database.bind.dialect.name == "sqlite":
            post_time = func.strftime("%Y-%m-%d %H:%M:%f", Post.created_at)
            cursor_time = func.strftime("%Y-%m-%d %H:%M:%f", cursor_created_at)
        else:
            post_time = Post.created_at
            cursor_time = cursor_created_at
        conditions.append(
            or_(
                post_time < cursor_time,
                (post_time == cursor_time) & (Post.id < cursor_post_id),
            )
        )

    rows = database.execute(
        select(Post, User, Community)
        .join(User, User.id == Post.author_id)
        # A personal update has no community row; the template branches on that.
        .outerjoin(Community, Community.id == Post.community_id)
        .where(*conditions)
        .order_by(Post.created_at.desc(), Post.id.desc())
        .limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = encode_updates_cursor(page_rows[-1][0], window_end) if has_more else None
    return render(
        request,
        "updates.html",
        {
            "user": user,
            "source": source,
            "days": days,
            "limit": limit,
            "posts": page_rows,
            "count": len(page_rows),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "window_end": window_end,
        },
    )
