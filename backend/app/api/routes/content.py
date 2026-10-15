"""Posts, threaded comments, and author/owner removal."""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import (
    allow_request,
    authenticated_form_user,
    authenticated_user,
    client_key,
    current_session,
)
from app.api.responses import render
from app.core import events
from app.core.database import get_db
from app.models import Comment, ContentReport, ModerationEvent, Post, User
from app.core.scoping import account_scope, community_scope
from app.core.security import check_rate_limit
from app.services.authorization import (
    blocked_user_ids,
    community_role,
    find_personal_post,
    find_post,
    find_visible_community,
    is_blocked_between,
    require_community_manager,
    require_community_member,
)
from app.services.comments import comment_tree
from app.services.communities import add_moderation_event
from app.services.notifications import queue_notification
from app.services.outbox import enqueue_event

router = APIRouter()


@router.post("/communities/{slug}/posts")
def create_post(
    slug: str,
    request: Request,
    title: str = Form(),
    body: str = Form(""),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    require_community_member(database, community, user)
    title = title.strip()
    body = body.strip()
    if not check_rate_limit(client_key(request), "create-post", limit=5, seconds=60):
        return RedirectResponse(f"/communities/{slug}?error=slow-down", status_code=303)
    if not title or len(title) > 140 or len(body) > 10000:
        return RedirectResponse(f"/communities/{slug}?error=invalid-post", status_code=303)
    duplicate_since = datetime.now(UTC) - timedelta(hours=24)
    duplicate = database.scalar(
        select(Post.id).where(
            Post.community_id == community.id,
            Post.author_id == user.id,
            Post.title == title,
            Post.body == body,
            Post.created_at >= duplicate_since,
            Post.removed_at.is_(None),
        )
    )
    if duplicate is not None:
        return RedirectResponse(f"/communities/{slug}?error=duplicate-post", status_code=303)
    post = Post(community_id=community.id, author_id=user.id, title=title, body=body)
    database.add(post)
    database.flush()
    enqueue_event(
        database,
        event=events.POST_CREATED,
        aggregate_type="post",
        aggregate_id=post.id,
        scope=community_scope(community.id),
        payload={"community_id": str(community.id), "author_id": str(user.id)},
    )
    database.commit()
    return RedirectResponse(f"/communities/{slug}/posts/{post.id}", status_code=303)


@router.get("/communities/{slug}/posts/{post_id}", response_class=HTMLResponse)
def post_detail(
    slug: str,
    post_id: UUID,
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    community = find_visible_community(database, slug, user)
    role = community_role(database, community.id, user.id) if user else None
    post = find_post(database, community, post_id)
    manager = role in {"owner", "moderator"}
    if post.removed_at is not None and not manager:
        raise HTTPException(status_code=404, detail="Post not found.")
    if user and is_blocked_between(database, user.id, post.author_id):
        raise HTTPException(status_code=404, detail="Post not found.")
    author = database.get(User, post.author_id)
    hidden_author_ids = blocked_user_ids(database, user.id) if user else set()
    roots = comment_tree(database, post.id, hidden_author_ids)
    reported = set()
    if user:
        reported = set(
            database.execute(
                select(ContentReport.target_type, ContentReport.target_id).where(
                    ContentReport.reporter_id == user.id,
                    ContentReport.community_id == community.id,
                )
            ).all()
        )
    browser_session = current_session(request, database) if user else None
    events = []
    if manager:
        events = database.scalars(
            select(ModerationEvent)
            .where(
                ModerationEvent.community_id == community.id,
                ModerationEvent.target_id == post.id,
            )
            .order_by(ModerationEvent.created_at.desc())
            .limit(25)
        ).all()
    return render(
        request,
        "post_detail.html",
        {
            "user": user,
            "community": community,
            "post": post,
            "author": author,
            "role": role,
            "manager": manager,
            "comments": roots,
            "csrf_token": browser_session.csrf_token if browser_session else None,
            "reported": reported,
            "events": events,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/communities/{slug}/posts/{post_id}/comments")
def create_comment(
    slug: str,
    post_id: UUID,
    request: Request,
    body: str = Form(),
    parent_id: UUID | None = Form(default=None),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    require_community_member(database, community, user)
    post = find_post(database, community, post_id)
    if post.removed_at is not None:
        raise HTTPException(status_code=404, detail="Post not found.")
    if post.is_locked:
        raise HTTPException(status_code=409, detail="This discussion is locked.")
    if is_blocked_between(database, user.id, post.author_id):
        raise HTTPException(status_code=404, detail="Post not found.")
    body = body.strip()
    if not allow_request(request, "create-comment"):
        return RedirectResponse(
            f"/communities/{slug}/posts/{post.id}?error=slow-down", status_code=303
        )
    if not body or len(body) > 5000:
        return RedirectResponse(
            f"/communities/{slug}/posts/{post.id}?error=invalid-comment", status_code=303
        )
    if parent_id is not None:
        parent = database.get(Comment, parent_id)
        if parent is None or parent.post_id != post.id or parent.removed_at is not None:
            raise HTTPException(status_code=404, detail="Comment not found.")
        if is_blocked_between(database, user.id, parent.author_id):
            raise HTTPException(status_code=404, detail="Comment not found.")
    recipient_ids = {post.author_id}
    if parent_id is not None:
        recipient_ids.add(parent.author_id)
    comment = Comment(post_id=post.id, author_id=user.id, parent_id=parent_id, body=body)
    database.add(comment)
    database.flush()
    enqueue_event(
        database,
        event=events.COMMENT_CREATED,
        aggregate_type="comment",
        aggregate_id=comment.id,
        scope=community_scope(community.id),
        payload={
            "community_id": str(community.id),
            "post_id": str(post.id),
            "author_id": str(user.id),
        },
    )
    for recipient_id in recipient_ids:
        recipient = database.get(User, recipient_id)
        if recipient is not None:
            queue_notification(
                database,
                recipient,
                user,
                "reply",
                f"{user.display_name} replied in {community.name}.",
                f"/communities/{slug}/posts/{post.id}",
            )
    database.commit()
    return RedirectResponse(
        f"/communities/{slug}/posts/{post.id}#comment-{comment.id}", status_code=303
    )


@router.post("/communities/{slug}/posts/{post_id}/remove")
def remove_post(
    slug: str,
    post_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    post = find_post(database, community, post_id)
    role = community_role(database, community.id, user.id)
    if post.author_id != user.id and role not in {"owner", "moderator"}:
        raise HTTPException(status_code=403, detail="You cannot remove this post.")
    if post.removed_at is None:
        post.removed_at = datetime.now(UTC)
        post.removed_by_user_id = user.id
        if role in {"owner", "moderator"}:
            add_moderation_event(
                database,
                community.id,
                user.id,
                "post",
                post.id,
                "post_removed",
                event=events.POST_REMOVED,
            )
        database.commit()
    return RedirectResponse(f"/communities/{slug}", status_code=303)


@router.post("/communities/{slug}/posts/{post_id}/lock")
def set_post_lock(
    slug: str,
    post_id: UUID,
    request: Request,
    locked: bool = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    require_community_manager(database, community, user)
    post = find_post(database, community, post_id)
    if post.removed_at is not None:
        raise HTTPException(status_code=404, detail="Post not found.")
    if post.is_locked != locked:
        post.is_locked = locked
        add_moderation_event(
            database,
            community.id,
            user.id,
            "post",
            post.id,
            "post_locked" if locked else "post_unlocked",
            event=events.POST_LOCKED if locked else events.POST_UNLOCKED,
        )
        database.commit()
    return RedirectResponse(f"/communities/{slug}/posts/{post.id}", status_code=303)


@router.post("/communities/{slug}/posts/{post_id}/comments/{comment_id}/remove")
def remove_comment(
    slug: str,
    post_id: UUID,
    comment_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    post = find_post(database, community, post_id)
    comment = database.get(Comment, comment_id)
    if comment is None or comment.post_id != post.id or comment.removed_at is not None:
        raise HTTPException(status_code=404, detail="Comment not found.")
    role = community_role(database, community.id, user.id)
    if comment.author_id != user.id and role not in {"owner", "moderator"}:
        raise HTTPException(status_code=403, detail="You cannot remove this comment.")
    comment.removed_at = datetime.now(UTC)
    comment.removed_by_user_id = user.id
    if role in {"owner", "moderator"}:
        add_moderation_event(
            database,
            community.id,
            user.id,
            "comment",
            comment.id,
            "comment_removed",
            event=events.COMMENT_REMOVED,
        )
    database.commit()
    return RedirectResponse(f"/communities/{slug}/posts/{post.id}#comments", status_code=303)


# A personal update belongs to its author instead of a community. The audience is the
# author's followers, so there is no membership to check and no moderator to consult: the
# author is the only privileged reader, and every refusal is a non-revealing 404.
@router.post("/posts")
def create_personal_post(
    request: Request,
    title: str = Form(""),
    body: str = Form(""),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    title = title.strip()
    body = body.strip()
    if not check_rate_limit(client_key(request), "create-post", limit=5, seconds=60):
        return RedirectResponse("/profile?error=slow-down", status_code=303)
    # The text carries the update; the title is optional context for it.
    if len(title) > 140 or not body or len(body) > 10000:
        return RedirectResponse("/profile?error=invalid-post", status_code=303)
    duplicate_since = datetime.now(UTC) - timedelta(hours=24)
    duplicate = database.scalar(
        select(Post.id).where(
            Post.community_id.is_(None),
            Post.author_id == user.id,
            Post.title == (title or None),
            Post.body == body,
            Post.created_at >= duplicate_since,
            Post.removed_at.is_(None),
        )
    )
    if duplicate is not None:
        return RedirectResponse("/profile?error=duplicate-post", status_code=303)
    post = Post(community_id=None, author_id=user.id, title=title or None, body=body)
    database.add(post)
    database.flush()
    enqueue_event(
        database,
        event=events.POST_CREATED,
        aggregate_type="post",
        aggregate_id=post.id,
        scope=account_scope(user.id),
        payload={"author_id": str(user.id)},
    )
    database.commit()
    return RedirectResponse(f"/posts/{post.id}", status_code=303)


@router.get("/posts/{post_id}", response_class=HTMLResponse)
def personal_post_detail(
    post_id: UUID,
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    post = find_personal_post(database, post_id, user)
    hidden_author_ids = blocked_user_ids(database, user.id) if user else set()
    browser_session = current_session(request, database) if user else None
    return render(
        request,
        "personal_post.html",
        {
            "user": user,
            "post": post,
            "author": database.get(User, post.author_id),
            "comments": comment_tree(database, post.id, hidden_author_ids),
            "csrf_token": browser_session.csrf_token if browser_session else None,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/posts/{post_id}/remove")
def remove_personal_post(
    post_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    post = find_personal_post(database, post_id, user)
    if post.author_id != user.id:
        raise HTTPException(status_code=403, detail="You cannot remove this post.")
    if post.removed_at is None:
        post.removed_at = datetime.now(UTC)
        post.removed_by_user_id = user.id
        enqueue_event(
            database,
            event=events.POST_REMOVED,
            aggregate_type="post",
            aggregate_id=post.id,
            scope=account_scope(user.id),
            payload={"author_id": str(user.id)},
        )
        database.commit()
    return RedirectResponse("/profile", status_code=303)


@router.post("/posts/{post_id}/comments")
def create_personal_comment(
    post_id: UUID,
    request: Request,
    body: str = Form(),
    parent_id: UUID | None = Form(default=None),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    post = find_personal_post(database, post_id, user)
    if post.removed_at is not None:
        raise HTTPException(status_code=404, detail="Post not found.")
    body = body.strip()
    if not allow_request(request, "create-comment"):
        return RedirectResponse(f"/posts/{post.id}?error=slow-down", status_code=303)
    if not body or len(body) > 5000:
        return RedirectResponse(f"/posts/{post.id}?error=invalid-comment", status_code=303)
    parent = None
    if parent_id is not None:
        parent = database.get(Comment, parent_id)
        if parent is None or parent.post_id != post.id or parent.removed_at is not None:
            raise HTTPException(status_code=404, detail="Comment not found.")
        if is_blocked_between(database, user.id, parent.author_id):
            raise HTTPException(status_code=404, detail="Comment not found.")
    comment = Comment(post_id=post.id, author_id=user.id, parent_id=parent_id, body=body)
    database.add(comment)
    database.flush()
    enqueue_event(
        database,
        event=events.COMMENT_CREATED,
        aggregate_type="comment",
        aggregate_id=comment.id,
        scope=account_scope(post.author_id),
        payload={"post_id": str(post.id), "author_id": str(user.id)},
    )
    recipients = {post.author_id} if parent is None else {post.author_id, parent.author_id}
    for recipient_id in recipients:
        recipient = database.get(User, recipient_id)
        if recipient is not None:
            queue_notification(
                database,
                recipient,
                user,
                "reply",
                f"{user.display_name} replied to an update.",
                f"/posts/{post.id}",
            )
    database.commit()
    return RedirectResponse(f"/posts/{post.id}#comment-{comment.id}", status_code=303)


@router.post("/posts/{post_id}/comments/{comment_id}/remove")
def remove_personal_comment(
    post_id: UUID,
    comment_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    post = find_personal_post(database, post_id, user)
    comment = database.get(Comment, comment_id)
    if comment is None or comment.post_id != post.id or comment.removed_at is not None:
        raise HTTPException(status_code=404, detail="Comment not found.")
    # The author owns the thread, so they can clear a reply from it as well as their own.
    if comment.author_id != user.id and post.author_id != user.id:
        raise HTTPException(status_code=403, detail="You cannot remove this comment.")
    comment.removed_at = datetime.now(UTC)
    comment.removed_by_user_id = user.id
    enqueue_event(
        database,
        event=events.COMMENT_REMOVED,
        aggregate_type="comment",
        aggregate_id=comment.id,
        scope=account_scope(post.author_id),
        payload={"post_id": str(post.id), "author_id": str(user.id)},
    )
    database.commit()
    return RedirectResponse(f"/posts/{post.id}#comments", status_code=303)
