"""Posts, threaded comments, and author/owner removal."""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from app.api.deps import (
    allow_request,
    authenticated_form_user,
    authenticated_user,
    client_key,
    current_session,
    form_context,
)
from app.api.responses import render
from app.core import events
from app.core.database import get_db
from app.models import AUDIENCES, Comment, ContentReport, ModerationEvent, Post, User
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
from app.services.moderation import appeal_for, platform_removal_report
from app.services.notifications import queue_notification
from app.services.outbox import enqueue_event
from app.services import attachments as attachment_service

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
        "posts/community.html",
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
    return_to: str = Form(""),
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
    return RedirectResponse(return_page(return_to, f"/communities/{slug}"), status_code=303)


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


# Author-side post actions (publish, withdraw) return to the page that submitted them, so the
# writer stays where they were. Anything unrecognized falls back to the action's own sensible
# page, which keeps the choice from becoming an open redirect.
RETURN_PAGES = {"/posts/new", "/workspace"}


def return_page(return_to: str, fallback: str) -> str:
    return return_to if return_to in RETURN_PAGES else fallback


# The compose view the dashboard's "New post" entry opens. It is registered before
# `/posts/{post_id}`, which would otherwise claim the literal path as a post id.
@router.get("/posts/new", response_class=HTMLResponse)
def compose_personal_post(
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    browser_session = current_session(request, database)
    return render(
        request,
        "posts/new.html",
        form_context(
            browser_session,
            user=user,
            error=request.query_params.get("error"),
            media=attachment_service.composer_limits(),
        ),
    )


# A personal update belongs to its author instead of a community. The audience is the
# author's followers, so there is no membership to check and no moderator to consult: the
# author is the only privileged reader, and every refusal is a non-revealing 404.
@router.post("/posts")
def create_personal_post(
    request: Request,
    title: str = Form(""),
    body: str = Form(""),
    audience: str = Form("followers"),
    csrf_token: str = Form(),
    return_to: str = Form(""),
    attachment_ids: list[UUID] = Form(default=[]),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    title = title.strip()
    body = body.strip()
    origin = return_page(return_to, "/posts/new")
    if not check_rate_limit(client_key(request), "create-post", limit=5, seconds=60):
        return RedirectResponse(f"{origin}?error=slow-down", status_code=303)
    # The text carries the update, but a photo, a clip, or a track can be the whole of it:
    # the rule is "something to publish", not "something to read".
    has_media = bool(attachment_ids)
    if len(title) > 140 or (not body and not has_media) or len(body) > 10000:
        return RedirectResponse(f"{origin}?error=invalid-post", status_code=303)
    if audience not in AUDIENCES:
        return RedirectResponse(f"{origin}?error=invalid-audience", status_code=303)
    if has_media and not attachment_service.can_attach_count(attachment_ids):
        return RedirectResponse(f"{origin}?error=invalid-media", status_code=303)
    # Duplicate detection is about repeated words. A media-only update has no words, so two
    # photo posts in a day are two posts, not one sent twice.
    if body:
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
            return RedirectResponse(f"{origin}?error=duplicate-post", status_code=303)
    post = Post(
        community_id=None, author_id=user.id, title=title or None, body=body, audience=audience
    )
    database.add(post)
    database.flush()
    try:
        attachment_service.attach_to_post(
            database, post=post, user=user, attachment_ids=attachment_ids
        )
    except HTTPException:
        # Nothing is published: the transaction is rolled back and the composer keeps the
        # text the reader wrote.
        database.rollback()
        return RedirectResponse(f"{origin}?error=invalid-media", status_code=303)
    enqueue_event(
        database,
        event=events.POST_CREATED,
        aggregate_type="post",
        aggregate_id=post.id,
        scope=account_scope(user.id),
        payload={"author_id": str(user.id), "media": len(attachment_ids)},
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
    # Only the author may question a removal, and only a removal this platform queue decided:
    # a withdrawal of one's own, or a community moderator's decision, has nothing to appeal to.
    appeal = None
    appealable = False
    if user is not None and post.removed_at is not None and user.id == post.author_id:
        if platform_removal_report(database, "post", post.id) is not None:
            appeal = appeal_for(database, "post", post.id, user.id)
            appealable = appeal is None
    reported = set()
    if user is not None:
        # The forms the reader has already used, so the page says "Reported" instead of
        # offering the same report twice.
        reported = set(
            database.execute(
                select(ContentReport.target_type, ContentReport.target_id).where(
                    ContentReport.reporter_id == user.id,
                    ContentReport.community_id.is_(None),
                    or_(
                        and_(
                            ContentReport.target_type == "post",
                            ContentReport.target_id == post.id,
                        ),
                        and_(
                            ContentReport.target_type == "comment",
                            ContentReport.target_id.in_(
                                select(Comment.id).where(Comment.post_id == post.id)
                            ),
                        ),
                    ),
                )
            ).all()
        )
    return render(
        request,
        "posts/personal.html",
        {
            "user": user,
            "post": post,
            "author": database.get(User, post.author_id),
            "media": [
                attachment_service.attachment_payload(item)
                for item in attachment_service.attachments_for_post(database, post.id)
            ],
            "comments": comment_tree(database, post.id, hidden_author_ids),
            "csrf_token": browser_session.csrf_token if browser_session else None,
            "reported": reported,
            "appeal": appeal,
            "appealable": appealable,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/posts/{post_id}/remove")
def remove_personal_post(
    post_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    return_to: str = Form(""),
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
    # A withdrawal returns to the workspace, where the row now reads as withdrawn.
    return RedirectResponse(return_page(return_to, "/workspace"), status_code=303)


@router.post("/posts/{post_id}/audience")
def set_personal_post_audience(
    post_id: UUID,
    request: Request,
    audience: str = Form(),
    csrf_token: str = Form(),
    return_to: str = Form(""),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    post = find_personal_post(database, post_id, user)
    if post.author_id != user.id:
        raise HTTPException(status_code=403, detail="You cannot change this post.")
    if post.removed_at is not None:
        raise HTTPException(status_code=404, detail="Post not found.")
    if audience not in AUDIENCES:
        raise HTTPException(status_code=422, detail="Choose a valid audience.")
    if post.audience != audience:
        post.audience = audience
        # Who may read an update is a privacy boundary, so the change is recorded even though
        # the row itself only holds the value it now has.
        enqueue_event(
            database,
            event=events.POST_AUDIENCE_CHANGED,
            aggregate_type="post",
            aggregate_id=post.id,
            scope=account_scope(user.id),
            payload={"author_id": str(user.id), "audience": audience},
        )
        database.commit()
    return RedirectResponse(return_page(return_to, "/workspace"), status_code=303)


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
    if post.is_locked:
        raise HTTPException(status_code=409, detail="This discussion is locked.")
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
