"""Profile, preferences, and the account data lifecycle."""

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
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
    set_theme_cookie,
)
from app.api.responses import relative_target, render
from app.core.theme import THEME_CHOICES
from app.core.appearance import BACKGROUND_VALUES, DENSITY_VALUES, PRESET_VALUES
from app.core import events
from app.core.database import get_db
from app.core.media import UnsupportedMedia, image_type, strip_image_metadata
from app.core.scoping import account_scope
from app.core.storage import StorageError, get_storage
from app.core.scoping import platform_scope
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
from app.services import attachments as attachment_service
from app.services.attachments import delete_attachments_for_owner
from app.services.audit import record_audit
from app.services.communities import delete_unowned_community, record_community_audit
from app.services.outbox import enqueue_event
from app.services import workspace

router = APIRouter()

logger = logging.getLogger(__name__)

AVATAR_MAX_BYTES = 5 * 1024 * 1024


def _avatar_key(user: User) -> str:
    """A fresh key per upload, so the public URL changes and the cache cannot serve the old one."""
    return f"avatars/{user.id}/{uuid4().hex}"


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
    # The profile is identity and the account fields; everything the reader has published, and
    # the withdraw action for it, lives on the workspace.
    return render(request, "account/profile.html", form_context(browser_session, user=user))


# The workspace manages everything the reader has published: a bounded keyset window over both
# kinds of post, with the author's own withdraw action on each row.
@router.get("/workspace", response_class=HTMLResponse)
def workspace_page(
    request: Request,
    source: str = "all",
    cursor: str | None = None,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    browser_session = current_session(request, database)
    if user is None or browser_session is None:
        return RedirectResponse("/login", status_code=303)
    if source not in workspace.SOURCES:
        raise HTTPException(status_code=422, detail="Choose a valid filter.")
    posts, next_cursor = workspace.list_posts(
        database,
        user.id,
        source,
        workspace.decode_cursor(cursor) if cursor else None,
        workspace.PAGE_SIZE,
    )
    return render(
        request,
        "account/workspace.html",
        form_context(
            browser_session,
            user=user,
            source=source,
            posts=posts,
            media=attachment_service.post_media_payloads(
                database, [post.id for post, _community, _openable in posts if not post.removed_at]
            ),
            count=len(posts),
            total=workspace.count_posts(database, user.id, source),
            next_cursor=next_cursor,
        ),
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
            "account/settings.html",
            form_context(
                browser_session,
                user=user,
                error="Display name must be 1–80 characters and bio must be at most 280.",
            ),
            status_code=422,
        )
    user.display_name = display_name
    user.bio = bio
    enqueue_event(
        database,
        event=events.ACCOUNT_PROFILE_CHANGED,
        aggregate_type="user",
        aggregate_id=user.id,
        scope=platform_scope(),
        payload={"user_id": str(user.id)},
    )
    database.commit()
    if request.headers.get("HX-Request") == "true":
        return render(request, "partials/profile_saved.html", {"message": "Profile saved."})
    return RedirectResponse("/profile?saved=1", status_code=303)


@router.post("/profile/avatar")
async def update_avatar(
    request: Request,
    avatar: UploadFile = File(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    """Replace an avatar with a normalized image; bytes never enter the database."""
    user, _ = authenticated_form_user(request, database, csrf_token)
    payload = await avatar.read(AVATAR_MAX_BYTES + 1)
    if not payload:
        return RedirectResponse("/profile?avatar_error=empty", status_code=303)
    if len(payload) > AVATAR_MAX_BYTES:
        return RedirectResponse("/profile?avatar_error=size", status_code=303)
    try:
        detected = image_type(payload)
        if detected is None:
            raise UnsupportedMedia("not an image")
        normalized = strip_image_metadata(payload, detected)
    except UnsupportedMedia:
        return RedirectResponse("/profile?avatar_error=image", status_code=303)
    storage = get_storage()
    previous_key = user.avatar_key
    key = _avatar_key(user)
    try:
        storage.put_bytes(key, normalized, content_type=detected)
    except StorageError as error:
        logger.warning(
            "could not store an uploaded avatar",
            extra={
                "event": "account.avatar_store_failed",
                "user_id": str(user.id),
                "error": str(error),
            },
        )
        return RedirectResponse("/profile?avatar_error=storage", status_code=303)
    user.avatar_key = key
    user.avatar_mime_type = detected
    database.commit()
    if previous_key and previous_key != key:
        # The new reference is durable; the superseded object is only a reclamation.
        try:
            storage.delete(previous_key)
        except StorageError:
            logger.warning(
                "could not remove the replaced avatar object",
                extra={"event": "account.avatar_replacement_failed", "user_id": str(user.id)},
            )
    return RedirectResponse("/profile?avatar_saved=1", status_code=303)


@router.post("/profile/avatar/remove")
def remove_avatar(
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    """Clear the avatar reference so the universal initial fallback is rendered."""
    user, _ = authenticated_form_user(request, database, csrf_token)
    if user.avatar_key:
        try:
            get_storage().delete(user.avatar_key)
        except StorageError:
            # The reference is still revoked even if a storage retry is needed later.
            pass
    user.avatar_key = None
    user.avatar_mime_type = None
    database.commit()
    return RedirectResponse("/profile?avatar_removed=1", status_code=303)


@router.get("/avatars/{user_id}")
def avatar_image(user_id: UUID, database: Session = Depends(get_db)):
    """Return a user's sanitized avatar; profile avatars are intentionally public."""
    user = database.get(User, user_id)
    if user is None or not user.is_active or not user.avatar_key or not user.avatar_mime_type:
        raise HTTPException(status_code=404, detail="Avatar not found.")
    try:
        payload = get_storage().get_bytes(user.avatar_key)
    except StorageError:
        raise HTTPException(status_code=503, detail="Avatar temporarily unavailable.") from None
    return Response(
        content=payload,
        media_type=user.avatar_mime_type,
        # A per-upload version makes a replacement a new URL; the short lifetime keeps a
        # removed avatar from lingering in a reader's cache.
        headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/theme")
def set_theme(
    request: Request,
    theme: str = Form(),
    next: str | None = Form(default=None),
    database: Session = Depends(get_db),
):
    """Remember a theme choice on this device, and on the account when there is one.

    No CSRF token: the only effect is the caller's own display preference, and a cross-site
    form can neither read the result nor gain anything from it. Every mutating route that
    changes shared state keeps its token.
    """
    if theme not in THEME_CHOICES:
        raise HTTPException(status_code=422, detail="Choose system, light, or dark.")
    user = authenticated_user(request, database)
    if user is not None and user.theme != theme:
        user.theme = theme
        database.commit()
    if request.headers.get("X-Theme-Update") == "true":
        response: Response = Response(status_code=204)
    else:
        response = RedirectResponse(relative_target(next) or "/", status_code=303)
    set_theme_cookie(response, theme)
    return response


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, database: Session = Depends(get_db)):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    browser_session = current_session(request, database)
    return render(
        request,
        "account/settings.html",
        form_context(
            browser_session,
            user=user,
            saved=request.query_params.get("saved"),
            password_changed=request.query_params.get("password"),
        ),
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
    notify_calls: bool = Form(False),
    accept_calls: bool = Form(False),
    theme: str = Form("system"),
    appearance_preset: str = Form("forest"),
    background_style: str = Form("paper"),
    display_density: str = Form("comfortable"),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, browser_session = authenticated_form_user(request, database, csrf_token)
    if (
        updates_source not in {"all", "people", "communities"}
        or updates_days not in {1, 7, 30, 90}
        or updates_limit not in {1, 5, 10, 20, 50}
        or theme not in THEME_CHOICES
        or appearance_preset not in PRESET_VALUES
        or background_style not in BACKGROUND_VALUES
        or display_density not in DENSITY_VALUES
    ):
        return render(
            request,
            "account/settings.html",
            form_context(browser_session, user=user, error="Choose valid settings."),
            status_code=422,
        )
    user.updates_source = updates_source
    user.updates_days = updates_days
    user.updates_limit = updates_limit
    user.quiet_mode = quiet_mode
    user.notify_replies = notify_replies
    user.notify_moderation = notify_moderation
    user.notify_calls = notify_calls
    user.accept_calls = accept_calls
    user.theme = theme
    user.appearance_preset = appearance_preset
    user.background_style = background_style
    user.display_density = display_density
    database.commit()
    response = RedirectResponse("/settings?saved=1", status_code=303)
    # Signing out keeps the same look on this device.
    set_theme_cookie(response, theme)
    return response


@router.post("/account/password")
def change_password(
    request: Request,
    current_password: str = Form(),
    new_password: str = Form(),
    new_password_confirmation: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
) -> Response:
    """Change the password of the signed-in account.

    The current password is required even though the session is already authenticated: it is
    what stops a borrowed tab from becoming a permanent takeover. A successful change signs
    out every other session and retires any unused reset link, because both were obtained
    with the old credential.
    """
    user, browser_session = authenticated_form_user(request, database, csrf_token)
    if not allow_request(request, "change-password"):
        return _password_rejected(
            request, browser_session, user, "Too many attempts. Try again later.", 429
        )
    if not verify_password(current_password, user.password_hash):
        return _password_rejected(
            request, browser_session, user, "The current password is incorrect.", 403
        )
    if verify_password(new_password, user.password_hash):
        return _password_rejected(
            request, browser_session, user, "Choose a password that differs from the current one."
        )
    # The same floor the reset flow applies: both forms are setting a replacement credential.
    if len(new_password) < 12 or len(new_password.encode("utf-8")) > 128:
        return _password_rejected(
            request, browser_session, user, "Use a password between 12 and 128 bytes."
        )
    if new_password != new_password_confirmation:
        return _password_rejected(request, browser_session, user, "New passwords do not match.")

    user.password_hash = hash_password(new_password)
    database.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None),
    ).update({PasswordResetToken.used_at: datetime.now(UTC)})
    # This session stays signed in; every other device must prove the new password.
    database.query(UserSession).filter(
        UserSession.user_id == user.id,
        UserSession.id != browser_session.id,
    ).delete(synchronize_session=False)
    database.commit()
    return RedirectResponse("/settings?password=1", status_code=303)


def _password_rejected(
    request: Request,
    browser_session: UserSession,
    user: User,
    message: str,
    status_code: int = 422,
) -> Response:
    # The message is rendered beside the password form rather than at the top of a long page.
    return render(
        request,
        "account/settings.html",
        form_context(browser_session, user=user, password_error=message),
        status_code=status_code,
    )


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
        # A personal update has no community row, and it is still the author's data.
        .outerjoin(Community, Community.id == Post.community_id)
        .where(Post.author_id == user.id)
    ).all()
    authored_comments = database.execute(
        select(Comment, Post, Community)
        .join(Post, Post.id == Comment.post_id)
        .outerjoin(Community, Community.id == Post.community_id)
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
                "community": community.slug if community else None,
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
                "community": community.slug if community else None,
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
        headers={"Content-Disposition": 'attachment; filename="social-plus-export.json"'},
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
            "account/settings.html",
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
            "account/profile.html",
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
        record_community_audit(
            database,
            community,
            event=events.COMMUNITY_OWNERSHIP_TRANSFERRED,
            action="owner_transferred",
            actor_user_id=None,
            target_user_id=successor.id,
            details="Ownership transferred after the owner's account was deleted.",
            before_state={"owner_user_id": str(user.id)},
            after_state={"owner_user_id": str(successor.id)},
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
    # Attachment and avatar objects go before their reference. Access is revoked either way,
    # but deleting the object first means the bytes do not outlive the record that made them
    # reachable.
    if user.avatar_key:
        try:
            get_storage().delete(user.avatar_key)
        except StorageError:
            logger.warning(
                "could not remove the avatar object of a deleted account",
                extra={"event": "account.avatar_delete_failed", "user_id": str(user.id)},
            )
    user.avatar_key = None
    user.avatar_mime_type = None
    delete_attachments_for_owner(database, owner_user_id=user.id)
    user.email = f"deleted+{user.id.hex}@deleted.invalid"
    user.username = f"deleted_{user.id.hex[:16]}"
    user.display_name = "Deleted account"
    user.bio = ""
    user.password_hash = hash_password(new_token())
    user.is_active = False
    record_audit(
        database,
        action=events.ACCOUNT_DELETED,
        scope=account_scope(user.id),
        actor_user_id=user.id,
        target_type="account",
        target_id=user.id,
        reason="Owner confirmed deletion with their current password.",
    )
    enqueue_event(
        database,
        event=events.ACCOUNT_DELETED,
        aggregate_type="account",
        aggregate_id=user.id,
        scope=account_scope(user.id),
        payload={"username": user.username},
    )
    database.commit()

    response = RedirectResponse("/account-deleted", status_code=303)
    response.delete_cookie(session_cookie_name(), path="/")
    return response


@router.get("/account-deleted", response_class=HTMLResponse)
def account_deleted_page(request: Request):
    return render(request, "account/deleted.html", {"user": None})
