"""Profile, preferences, and the account data lifecycle."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session
from app.api.deps import (
    allow_request,
    authenticated_form_user,
    authenticated_user,
    current_session,
    form_context,
    require_csrf,
    session_cookie_name,
)
from app.api.responses import render
from app.core.database import get_db
from app.models import (
    Comment,
    Community,
    CommunityAuditEvent,
    CommunityMembership,
    ContentReport,
    Conversation,
    ConversationEvent,
    ConversationMember,
    ConversationMessage,
    ModerationEvent,
    Notification,
    PasswordResetToken,
    Post,
    User,
    UserRelationship,
    UserSession,
)
from app.core.security import hash_password, new_token, verify_password
from app.services.communities import delete_unowned_community

router = APIRouter()


@router.get("/profile", response_class=HTMLResponse)
def profile_page(
    request: Request,
    response: Response,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    browser_session = current_session(request, database)
    return render(
        request,
        "profile.html",
        form_context(browser_session, user=user),
    )


@router.post("/profile", response_class=HTMLResponse)
def update_profile(
    request: Request,
    display_name: str = Form(),
    bio: str = Form(""),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    browser_session = current_session(request, database)
    if user is None or browser_session is None:
        return RedirectResponse("/login", status_code=303)
    require_csrf(request, browser_session, csrf_token)
    display_name = display_name.strip()
    bio = bio.strip()
    if not display_name or len(display_name) > 80 or len(bio) > 280:
        return render(
            request,
            "settings.html",
            form_context(
                browser_session,
                user=user,
                error="Display name must be 1–80 characters and bio must be at most 280.",
            ),
            status_code=422,
        )
    user.display_name = display_name
    user.bio = bio
    database.commit()
    if request.headers.get("HX-Request") == "true":
        return render(request, "partials/profile_saved.html", {"message": "Profile saved."})
    return RedirectResponse("/profile?saved=1", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, database: Session = Depends(get_db)):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    browser_session = current_session(request, database)
    return render(
        request,
        "settings.html",
        form_context(browser_session, user=user, saved=request.query_params.get("saved")),
    )


@router.post("/settings")
def update_settings(
    request: Request,
    updates_source: str = Form(),
    updates_days: int = Form(),
    updates_limit: int = Form(),
    quiet_mode: bool = Form(False),
    notify_replies: bool = Form(False),
    notify_moderation: bool = Form(False),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, browser_session = authenticated_form_user(request, database, csrf_token)
    if (
        updates_source not in {"all", "people", "communities"}
        or updates_days not in {1, 7, 30, 90}
        or updates_limit not in {1, 5, 10, 20, 50}
    ):
        return render(
            request,
            "settings.html",
            form_context(browser_session, user=user, error="Choose valid update defaults."),
            status_code=422,
        )
    user.updates_source = updates_source
    user.updates_days = updates_days
    user.updates_limit = updates_limit
    user.quiet_mode = quiet_mode
    user.notify_replies = notify_replies
    user.notify_moderation = notify_moderation
    database.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.get("/account/export")
def export_account(request: Request, database: Session = Depends(get_db)):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    memberships = database.execute(
        select(CommunityMembership, Community)
        .join(Community, Community.id == CommunityMembership.community_id)
        .where(CommunityMembership.user_id == user.id)
    ).all()
    authored_posts = database.execute(
        select(Post, Community)
        .join(Community, Community.id == Post.community_id)
        .where(Post.author_id == user.id)
    ).all()
    authored_comments = database.execute(
        select(Comment, Post, Community)
        .join(Post, Post.id == Comment.post_id)
        .join(Community, Community.id == Post.community_id)
        .where(Comment.author_id == user.id)
    ).all()
    relationships = database.scalars(
        select(UserRelationship).where(
            or_(
                UserRelationship.source_user_id == user.id,
                UserRelationship.target_user_id == user.id,
            )
        )
    ).all()
    reports = database.scalars(
        select(ContentReport).where(ContentReport.reporter_id == user.id)
    ).all()
    community_events = database.scalars(
        select(CommunityAuditEvent).where(CommunityAuditEvent.actor_user_id == user.id)
    ).all()
    moderation_events = database.scalars(
        select(ModerationEvent).where(ModerationEvent.moderator_id == user.id)
    ).all()
    active_memberships = database.scalars(
        select(ConversationMember).where(
            ConversationMember.user_id == user.id,
            ConversationMember.left_at.is_(None),
        )
    ).all()
    conversation_ids = [membership.conversation_id for membership in active_memberships]
    conversations = database.scalars(
        select(Conversation).where(Conversation.id.in_(conversation_ids))
    ).all()
    messages = database.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id.in_(conversation_ids))
        .order_by(ConversationMessage.created_at, ConversationMessage.id)
    ).all()
    conversation_events = database.scalars(
        select(ConversationEvent)
        .where(ConversationEvent.conversation_id.in_(conversation_ids))
        .order_by(ConversationEvent.created_at, ConversationEvent.id)
    ).all()
    notifications = database.scalars(
        select(Notification)
        .where(Notification.recipient_user_id == user.id)
        .order_by(Notification.created_at, Notification.id)
    ).all()

    payload = {
        "account": {
            "email": user.email,
            "username": user.username,
            "display_name": user.display_name,
            "bio": user.bio,
            "created_at": user.created_at,
            "quiet_mode": user.quiet_mode,
            "notify_replies": user.notify_replies,
            "notify_moderation": user.notify_moderation,
            "updates_source": user.updates_source,
            "updates_days": user.updates_days,
            "updates_limit": user.updates_limit,
        },
        "relationships": [
            {
                "source_user_id": relation.source_user_id,
                "target_user_id": relation.target_user_id,
                "kind": relation.kind,
                "created_at": relation.created_at,
            }
            for relation in relationships
        ],
        "community_memberships": [
            {
                "community_id": community.id,
                "name": community.name,
                "slug": community.slug,
                "role": membership.role,
                "joined_at": membership.joined_at,
            }
            for membership, community in memberships
        ],
        "posts": [
            {
                "id": post.id,
                "community": community.slug,
                "title": post.title,
                "body": post.body,
                "created_at": post.created_at,
                "removed_at": post.removed_at,
            }
            for post, community in authored_posts
        ],
        "comments": [
            {
                "id": comment.id,
                "post_id": post.id,
                "community": community.slug,
                "parent_id": comment.parent_id,
                "body": comment.body,
                "created_at": comment.created_at,
                "removed_at": comment.removed_at,
            }
            for comment, post, community in authored_comments
        ],
        "reports": [
            {
                "id": report.id,
                "target_type": report.target_type,
                "target_id": report.target_id,
                "reason": report.reason,
                "status": report.status,
                "created_at": report.created_at,
                "resolved_at": report.resolved_at,
            }
            for report in reports
        ],
        "community_audit_events": [
            {
                "community_id": event.community_id,
                "action": event.action,
                "details": event.details,
                "created_at": event.created_at,
            }
            for event in community_events
        ],
        "moderation_events": [
            {
                "community_id": event.community_id,
                "target_type": event.target_type,
                "target_id": event.target_id,
                "action": event.action,
                "details": event.details,
                "created_at": event.created_at,
            }
            for event in moderation_events
        ],
        "conversations": [
            {"id": conversation.id, "kind": conversation.kind, "title": conversation.title}
            for conversation in conversations
        ],
        "messages": [
            {
                "id": message.id,
                "conversation_id": message.conversation_id,
                "sender_id": message.sender_id,
                "body": message.body,
                "created_at": message.created_at,
            }
            for message in messages
        ],
        "conversation_events": [
            {
                "conversation_id": event.conversation_id,
                "action": event.action,
                "details": event.details,
                "created_at": event.created_at,
            }
            for event in conversation_events
        ],
        "notifications": [
            {
                "id": notification.id,
                "category": notification.category,
                "message": notification.message,
                "created_at": notification.created_at,
                "read_at": notification.read_at,
            }
            for notification in notifications
        ],
    }
    return JSONResponse(
        jsonable_encoder(payload),
        headers={"Content-Disposition": 'attachment; filename="soccom-export.json"'},
    )


@router.post("/account/delete")
def delete_account(
    request: Request,
    password: str = Form(),
    confirmation: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, browser_session = authenticated_form_user(request, database, csrf_token)
    if not allow_request(request, "delete-account"):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    if confirmation != "DELETE":
        return render(
            request,
            "settings.html",
            form_context(
                browser_session,
                user=user,
                error='Type "DELETE" exactly to confirm account deletion.',
            ),
            status_code=422,
        )
    if not verify_password(password, user.password_hash):
        return render(
            request,
            "profile.html",
            form_context(browser_session, user=user, error="The current password is incorrect."),
            status_code=403,
        )

    owned_communities = database.scalars(
        select(Community).where(Community.created_by == user.id)
    ).all()
    for community in owned_communities:
        successor_row = database.execute(
            select(CommunityMembership, User)
            .join(User, User.id == CommunityMembership.user_id)
            .where(
                CommunityMembership.community_id == community.id,
                CommunityMembership.user_id != user.id,
                User.is_active.is_(True),
            )
            .order_by(
                case((CommunityMembership.role == "moderator", 0), else_=1),
                CommunityMembership.joined_at,
            )
            .limit(1)
        ).first()
        if successor_row is None:
            delete_unowned_community(database, community)
            continue
        successor_membership, successor = successor_row
        successor_membership.role = "owner"
        community.created_by = successor.id
        database.add(
            CommunityAuditEvent(
                community_id=community.id,
                actor_user_id=None,
                target_user_id=successor.id,
                action="owner_transferred",
                details="Ownership transferred after the owner's account was deleted.",
            )
        )

    database.query(CommunityMembership).filter(CommunityMembership.user_id == user.id).delete(
        synchronize_session=False
    )
    database.query(UserRelationship).filter(
        or_(UserRelationship.source_user_id == user.id, UserRelationship.target_user_id == user.id)
    ).delete(synchronize_session=False)
    database.query(UserSession).filter(UserSession.user_id == user.id).delete(
        synchronize_session=False
    )
    database.query(PasswordResetToken).filter(PasswordResetToken.user_id == user.id).delete(
        synchronize_session=False
    )
    database.query(Notification).filter(Notification.recipient_user_id == user.id).delete(
        synchronize_session=False
    )
    database.query(ContentReport).filter(ContentReport.reporter_id == user.id).update(
        {ContentReport.reason: "Reporter account deleted."}, synchronize_session=False
    )
    user.email = f"deleted+{user.id.hex}@deleted.invalid"
    user.username = f"deleted_{user.id.hex[:16]}"
    user.display_name = "Deleted account"
    user.bio = ""
    user.password_hash = hash_password(new_token())
    user.is_active = False
    database.commit()

    response = RedirectResponse("/account-deleted", status_code=303)
    response.delete_cookie(session_cookie_name(), path="/")
    return response


@router.get("/account-deleted", response_class=HTMLResponse)
def account_deleted_page(request: Request):
    return render(request, "account_deleted.html", {"user": None})
