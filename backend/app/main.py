import logging
import re
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.mail import send_password_reset_email
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
    Post,
    PasswordResetToken,
    User,
    UserRelationship,
    UserSession,
)
from app.security import (
    DUMMY_PASSWORD_HASH,
    check_rate_limit,
    hash_password,
    new_token,
    session_expiry,
    token_digest,
    verify_password,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
SESSION_COOKIE = "soccom_session"
USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,24}$")


class ConversationSocketHub:
    def __init__(self):
        self.connections: dict[UUID, list[tuple[WebSocket, UUID]]] = {}

    async def connect(self, conversation_id: UUID, user_id: UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.setdefault(conversation_id, []).append((websocket, user_id))

    def disconnect(self, conversation_id: UUID, websocket: WebSocket) -> None:
        active = self.connections.get(conversation_id, [])
        self.connections[conversation_id] = [
            (socket, user_id) for socket, user_id in active if socket is not websocket
        ]
        if not self.connections[conversation_id]:
            self.connections.pop(conversation_id, None)

    async def disconnect_user(self, conversation_id: UUID, user_id: UUID) -> None:
        for websocket, connected_user_id in list(self.connections.get(conversation_id, [])):
            if connected_user_id == user_id:
                try:
                    await websocket.close(code=4403)
                except (WebSocketDisconnect, RuntimeError):
                    pass
                self.disconnect(conversation_id, websocket)

    async def broadcast(self, conversation_id: UUID, event: dict) -> None:
        for websocket, _ in list(self.connections.get(conversation_id, [])):
            try:
                await websocket.send_json(event)
            except (WebSocketDisconnect, RuntimeError):
                self.disconnect(conversation_id, websocket)


conversation_sockets = ConversationSocketHub()

app = FastAPI(title="Soccom", version="0.1.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://unpkg.com; "
        "style-src 'self' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


def render(request: Request, name: str, context: dict, status_code: int = 200):
    response = templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )
    pending_token = getattr(request.state, "pending_session_token", None)
    if pending_token:
        set_session_cookie(response, pending_token)
        request.state.pending_session_token = None
    return response


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def allow_request(request: Request, action: str) -> bool:
    return check_rate_limit(client_key(request), action)


def set_session_cookie(response: Response, raw_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        session_cookie_name(),
        raw_token,
        max_age=14 * 24 * 60 * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def session_cookie_name() -> str:
    return "__Host-soccom_session" if get_settings().cookie_secure else SESSION_COOKIE


def current_session(request: Request | WebSocket, database: Session) -> UserSession | None:
    raw_token = request.cookies.get(session_cookie_name())
    if not raw_token:
        return None
    browser_session = database.scalar(
        select(UserSession).where(UserSession.token_hash == token_digest(raw_token))
    )
    if browser_session is None:
        return None
    expiry = browser_session.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if expiry <= datetime.now(UTC):
        database.delete(browser_session)
        database.commit()
        return None
    return browser_session


def ensure_browser_session(request: Request, database: Session) -> UserSession:
    browser_session = current_session(request, database)
    if browser_session is not None:
        return browser_session
    raw_token = new_token()
    browser_session = UserSession(
        token_hash=token_digest(raw_token),
        csrf_token=new_token(),
        expires_at=session_expiry(),
    )
    database.add(browser_session)
    database.commit()
    request.state.pending_session_token = raw_token
    return browser_session


def authenticated_user(request: Request, database: Session) -> User | None:
    browser_session = current_session(request, database)
    if browser_session is None or browser_session.user_id is None:
        return None
    user = database.get(User, browser_session.user_id)
    return user if user and user.is_active else None


def require_csrf(request: Request, browser_session: UserSession, token: str) -> None:
    if not token or not secrets.compare_digest(token, browser_session.csrf_token):
        raise HTTPException(status_code=403, detail="The form expired. Reload and try again.")


def form_context(browser_session: UserSession, **values) -> dict:
    return {"csrf_token": browser_session.csrf_token, **values}


def begin_authenticated_session(database: Session, user: User, request: Request) -> RedirectResponse:
    previous = current_session(request, database)
    if previous is not None:
        database.delete(previous)
    raw_token = new_token()
    browser_session = UserSession(
        user_id=user.id,
        token_hash=token_digest(raw_token),
        csrf_token=new_token(),
        expires_at=session_expiry(),
    )
    database.add(browser_session)
    database.commit()
    response = RedirectResponse("/conversations", status_code=303)
    set_session_cookie(response, raw_token)
    return response


def invalid_form(request: Request, page: str, browser_session: UserSession, error: str, **context):
    return render(
        request,
        page,
        form_context(browser_session, error=error, **context),
        status_code=422,
    )


def authenticated_form_user(
    request: Request, database: Session, csrf_token: str
) -> tuple[User, UserSession]:
    browser_session = current_session(request, database)
    user = authenticated_user(request, database)
    if browser_session is None or user is None:
        raise HTTPException(status_code=403, detail="Sign in to continue.")
    require_csrf(request, browser_session, csrf_token)
    return user, browser_session


def community_role(database: Session, community_id, user_id) -> str | None:
    membership = database.get(CommunityMembership, (community_id, user_id))
    return membership.role if membership else None


def find_visible_community(database: Session, slug: str, user: User | None) -> Community:
    community = database.scalar(select(Community).where(Community.slug == slug))
    if community is None:
        raise HTTPException(status_code=404, detail="Community not found.")
    if community.visibility == "private" and (
        user is None or community_role(database, community.id, user.id) is None
    ):
        raise HTTPException(status_code=404, detail="Community not found.")
    return community


def is_blocked_between(database: Session, first_user_id, second_user_id) -> bool:
    return database.scalar(
        select(UserRelationship.source_user_id).where(
            UserRelationship.kind == "block",
            or_(
                (UserRelationship.source_user_id == first_user_id)
                & (UserRelationship.target_user_id == second_user_id),
                (UserRelationship.source_user_id == second_user_id)
                & (UserRelationship.target_user_id == first_user_id),
            ),
        )
    ) is not None


def active_conversation_member(
    database: Session, conversation_id: UUID, user_id: UUID
) -> ConversationMember:
    member = database.get(ConversationMember, (conversation_id, user_id))
    if member is None or member.left_at is not None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return member


def direct_conversation_key(first_user_id: UUID, second_user_id: UUID) -> str:
    pair = sorted((str(first_user_id), str(second_user_id)))
    return sha256(":".join(pair).encode("utf-8")).hexdigest()


def message_payload(message: ConversationMessage, sender: User) -> dict:
    return {
        "id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "sender_id": str(sender.id),
        "sender_name": sender.display_name,
        "sender_username": sender.username,
        "body": message.body,
        "created_at": message.created_at.isoformat(),
    }


def blocked_user_ids(database: Session, user_id) -> set[UUID]:
    relationships = database.scalars(
        select(UserRelationship).where(
            UserRelationship.kind == "block",
            or_(
                UserRelationship.source_user_id == user_id,
                UserRelationship.target_user_id == user_id,
            ),
        )
    ).all()
    return {
        relation.target_user_id if relation.source_user_id == user_id else relation.source_user_id
        for relation in relationships
    }


def require_community_member(database: Session, community: Community, user: User) -> str:
    role = community_role(database, community.id, user.id)
    if role is None:
        raise HTTPException(status_code=403, detail="Join the community to participate.")
    return role


def find_post(database: Session, community: Community, post_id: UUID) -> Post:
    post = database.get(Post, post_id)
    if post is None or post.community_id != community.id:
        raise HTTPException(status_code=404, detail="Post not found.")
    return post


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


def delete_unowned_community(database: Session, community: Community) -> None:
    post_ids = select(Post.id).where(Post.community_id == community.id)
    database.query(ContentReport).filter(ContentReport.community_id == community.id).delete(
        synchronize_session=False
    )
    database.query(ModerationEvent).filter(ModerationEvent.community_id == community.id).delete(
        synchronize_session=False
    )
    database.query(CommunityAuditEvent).filter(
        CommunityAuditEvent.community_id == community.id
    ).delete(synchronize_session=False)
    database.query(Comment).filter(Comment.post_id.in_(post_ids)).delete(synchronize_session=False)
    database.query(Post).filter(Post.community_id == community.id).delete(synchronize_session=False)
    database.query(CommunityMembership).filter(
        CommunityMembership.community_id == community.id
    ).delete(synchronize_session=False)
    database.delete(community)


def add_moderation_event(
    database: Session,
    community_id: UUID,
    moderator_id: UUID,
    target_type: str,
    target_id: UUID,
    action: str,
    details: str = "",
) -> None:
    database.add(
        ModerationEvent(
            community_id=community_id,
            moderator_id=moderator_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            details=details,
        )
    )


def redirect_people(message: str | None = None) -> RedirectResponse:
    location = "/people"
    if message:
        location = f"{location}?message={quote(message)}"
    return RedirectResponse(location, status_code=303)


def require_community_manager(
    database: Session, community: Community, user: User, owner_only: bool = False
) -> str:
    role = community_role(database, community.id, user.id)
    allowed = {"owner"} if owner_only else {"owner", "moderator"}
    if role not in allowed:
        raise HTTPException(status_code=403, detail="You do not have permission to manage this community.")
    return role


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz(database: Session = Depends(get_db)):
    database.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, database: Session = Depends(get_db)):
    user = authenticated_user(request, database)
    if user is not None:
        return RedirectResponse("/conversations", status_code=303)
    return render(request, "home.html", {"user": user})


@app.get("/updates", response_class=HTMLResponse)
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
    if source in {"all", "people"}:
        source_conditions.append(Post.author_id.in_(followed_authors))
    if source in {"all", "communities"}:
        source_conditions.append(Post.community_id.in_(joined_communities))

    blocked_authors = select(UserRelationship.target_user_id).where(
        UserRelationship.source_user_id == user.id,
        UserRelationship.kind == "block",
    ).union(
        select(UserRelationship.source_user_id).where(
            UserRelationship.target_user_id == user.id,
            UserRelationship.kind == "block",
        )
    )
    conditions = [
        Post.removed_at.is_(None),
        Post.created_at >= window_start,
        Post.created_at <= window_end,
        User.is_active.is_(True),
        Post.author_id.not_in(blocked_authors),
        or_(Community.visibility == "public", Community.id.in_(joined_communities)),
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
        .join(Community, Community.id == Post.community_id)
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


@app.get("/register", response_class=HTMLResponse)
def register_page(
    request: Request,
    database: Session = Depends(get_db),
):
    if authenticated_user(request, database):
        return RedirectResponse("/conversations", status_code=303)
    browser_session = ensure_browser_session(request, database)
    return render(request, "register.html", form_context(browser_session))


@app.get("/notifications", response_class=HTMLResponse)
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


@app.get("/api/v1/notifications")
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
    return JSONResponse(
        jsonable_encoder({
            "notifications": [
                {
                    "id": str(notification.id),
                    "category": notification.category,
                    "message": notification.message,
                    "target_url": notification.target_url,
                    "created_at": notification.created_at,
                    "read_at": notification.read_at,
                }
                for notification, _actor in rows
            ],
            "count": len(rows),
            "has_more": has_more,
            "next_cursor": next_cursor,
        })
    )


@app.post("/notifications/{notification_id}/read")
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


@app.post("/notifications/read-all")
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


@app.post("/notifications/clear")
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


@app.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    email: str = Form(),
    username: str = Form(),
    display_name: str = Form(),
    password: str = Form(),
    password_confirmation: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    browser_session = current_session(request, database)
    if browser_session is None:
        raise HTTPException(status_code=403, detail="Reload the form and try again.")
    require_csrf(request, browser_session, csrf_token)
    if not allow_request(request, "register"):
        return invalid_form(request, "register.html", browser_session, "Too many attempts. Try again later.")

    email = email.strip().lower()
    username = username.strip().lower()
    display_name = display_name.strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 320:
        return invalid_form(request, "register.html", browser_session, "Enter a valid email address.")
    if not USERNAME_PATTERN.fullmatch(username):
        return invalid_form(
            request,
            "register.html",
            browser_session,
            "Username must be 3–24 characters using letters, numbers, or underscores.",
        )
    if not display_name or len(display_name) > 80:
        return invalid_form(request, "register.html", browser_session, "Display name must be 1–80 characters.")
    if len(password) < 8 or len(password.encode("utf-8")) > 128:
        return invalid_form(request, "register.html", browser_session, "Use a password between 8 and 128 bytes.")
    if password != password_confirmation:
        return invalid_form(request, "register.html", browser_session, "Passwords do not match.")
    if database.scalar(select(User.id).where((User.email == email) | (User.username == username))):
        return invalid_form(
            request,
            "register.html",
            browser_session,
            "That email or username is already in use.",
        )

    user = User(
        email=email,
        username=username,
        display_name=display_name,
        password_hash=hash_password(password),
    )
    database.add(user)
    try:
        database.commit()
    except Exception:
        database.rollback()
        logger.exception("Account registration failed")
        return invalid_form(request, "register.html", browser_session, "Could not create the account. Try again.")
    return begin_authenticated_session(database, user, request)


@app.get("/people", response_class=HTMLResponse)
def people_page(
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    relationships = database.scalars(
        select(UserRelationship).where(
            or_(
                UserRelationship.source_user_id == user.id,
                UserRelationship.target_user_id == user.id,
            )
        )
    ).all()
    following = {item.target_user_id for item in relationships if item.kind == "follow" and item.source_user_id == user.id}
    following_by = {item.source_user_id for item in relationships if item.kind == "follow" and item.target_user_id == user.id}
    blocked = {item.target_user_id for item in relationships if item.kind == "block" and item.source_user_id == user.id}
    blocked_by = {item.source_user_id for item in relationships if item.kind == "block" and item.target_user_id == user.id}
    users = database.scalars(
        select(User)
        .where(User.id != user.id, User.is_active.is_(True))
        .order_by(User.username)
        .limit(100)
    ).all()
    people = [
        {
            "user": person,
            "following": person.id in following,
            "follows_you": person.id in following_by,
            "blocked": person.id in blocked,
            "blocked_you": person.id in blocked_by,
        }
        for person in users
    ]
    browser_session = current_session(request, database)
    return render(
        request,
        "people.html",
        {
            "user": user,
            "people": people,
            "message": request.query_params.get("message"),
            "csrf_token": browser_session.csrf_token if browser_session else None,
        },
    )


@app.get("/conversations", response_class=HTMLResponse)
def conversation_inbox(
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    rows = database.execute(
        select(Conversation, func.max(ConversationMessage.created_at))
        .join(
            ConversationMember,
            and_(
                ConversationMember.conversation_id == Conversation.id,
                ConversationMember.user_id == user.id,
                ConversationMember.left_at.is_(None),
            ),
        )
        .outerjoin(ConversationMessage, ConversationMessage.conversation_id == Conversation.id)
        .group_by(Conversation.id)
        .order_by(func.coalesce(func.max(ConversationMessage.created_at), Conversation.created_at).desc())
        .limit(100)
    ).all()
    conversations = []
    for conversation, _ in rows:
        membership = database.get(ConversationMember, (conversation.id, user.id))
        member_rows = database.execute(
            select(ConversationMember, User)
            .join(User, User.id == ConversationMember.user_id)
            .where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.left_at.is_(None),
            )
        ).all()
        participants = [member_user for _, member_user in member_rows]
        peer = next((person for person in participants if person.id != user.id), None)
        latest = database.scalar(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(1)
        )
        unread_query = select(func.count(ConversationMessage.id)).where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.sender_id != user.id,
        )
        if membership.last_read_at is not None:
            unread_query = unread_query.where(ConversationMessage.created_at > membership.last_read_at)
        unread_count = database.scalar(unread_query) or 0
        blocked = bool(
            conversation.kind == "direct"
            and peer is not None
            and is_blocked_between(database, user.id, peer.id)
        )
        conversations.append(
            {
                "conversation": conversation,
                "peer": peer,
                "participants": participants,
                "latest": latest,
                "unread_count": unread_count,
                "blocked": blocked,
            }
        )
    browser_session = current_session(request, database)
    return render(
        request,
        "conversation_inbox.html",
        {
            "user": user,
            "conversations": conversations,
            "csrf_token": browser_session.csrf_token,
        },
    )


@app.post("/conversations/direct/{target_id}")
def start_direct_conversation(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    target = database.get(User, target_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot message yourself.")
    if is_blocked_between(database, user.id, target.id):
        return redirect_people("A block prevents direct messages.")
    direct_key = direct_conversation_key(user.id, target.id)
    conversation = database.scalar(
        select(Conversation).where(Conversation.direct_key == direct_key)
    )
    if conversation is None:
        conversation = Conversation(kind="direct", direct_key=direct_key, created_by=user.id)
        database.add(conversation)
        database.flush()
        database.add_all(
            [
                ConversationMember(
                    conversation_id=conversation.id, user_id=user.id, role="owner"
                ),
                ConversationMember(
                    conversation_id=conversation.id, user_id=target.id, role="member"
                ),
            ]
        )
        try:
            database.commit()
        except IntegrityError:
            database.rollback()
            conversation = database.scalar(
                select(Conversation).where(Conversation.direct_key == direct_key)
            )
            if conversation is None:
                raise
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@app.get("/conversations/new", response_class=HTMLResponse)
def new_group_conversation_page(
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    browser_session = current_session(request, database)
    return render(request, "conversation_new.html", form_context(browser_session, user=user))


@app.post("/conversations/groups")
def create_group_conversation(
    request: Request,
    title: str = Form(),
    usernames: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, browser_session = authenticated_form_user(request, database, csrf_token)
    title = title.strip()
    requested_usernames = list(
        dict.fromkeys(
            username.lower()
            for username in re.split(r"[,\s]+", usernames.strip())
            if username
        )
    )
    error = None
    if not title or len(title) > 80:
        error = "Group name must be 1–80 characters."
    elif not requested_usernames:
        error = "Add at least one participant by username."
    elif len(requested_usernames) > 19:
        error = "A group can have up to 20 participants, including you."
    elif user.username in requested_usernames:
        error = "You are already included in this group."
    participants = []
    if error is None:
        participants = database.scalars(
            select(User).where(
                User.username.in_(requested_usernames),
                User.is_active.is_(True),
            )
        ).all()
        found = {person.username for person in participants}
        if found != set(requested_usernames):
            error = "One or more usernames were not found."
        elif any(is_blocked_between(database, user.id, person.id) for person in participants):
            error = "A block prevents adding one of those participants."
    if error:
        return render(
            request,
            "conversation_new.html",
            form_context(browser_session, user=user, error=error, title=title, usernames=usernames),
            status_code=422,
        )
    conversation = Conversation(kind="group", title=title, created_by=user.id)
    database.add(conversation)
    database.flush()
    database.add(
        ConversationMember(conversation_id=conversation.id, user_id=user.id, role="owner")
    )
    for participant in participants:
        database.add(
            ConversationMember(
                conversation_id=conversation.id,
                user_id=participant.id,
                role="member",
            )
        )
        database.add(
            ConversationEvent(
                conversation_id=conversation.id,
                actor_user_id=user.id,
                subject_user_id=participant.id,
                action="member_added",
                details=f"@{participant.username} was added to the group",
            )
        )
    database.commit()
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@app.get("/conversations/{conversation_id}", response_class=HTMLResponse)
def conversation_detail(
    conversation_id: UUID,
    request: Request,
    before: UUID | None = None,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    membership = active_conversation_member(database, conversation_id, user.id)
    conversation = database.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    member_rows = database.execute(
        select(ConversationMember, User)
        .join(User, User.id == ConversationMember.user_id)
        .where(
            ConversationMember.conversation_id == conversation.id,
            ConversationMember.left_at.is_(None),
        )
        .order_by(ConversationMember.joined_at)
    ).all()
    participants = [
        {"membership": member, "user": participant}
        for member, participant in member_rows
    ]
    peer = next(
        (participant for participant in participants if participant["user"].id != user.id),
        None,
    )
    blocked = bool(
        conversation.kind == "direct"
        and peer
        and is_blocked_between(database, user.id, peer["user"].id)
    )
    message_query = select(ConversationMessage).where(
        ConversationMessage.conversation_id == conversation.id
    )
    if before is not None:
        cursor_message = database.get(ConversationMessage, before)
        if cursor_message is None or cursor_message.conversation_id != conversation.id:
            raise HTTPException(status_code=404, detail="Message not found.")
        message_query = message_query.where(
            or_(
                ConversationMessage.created_at < cursor_message.created_at,
                and_(
                    ConversationMessage.created_at == cursor_message.created_at,
                    ConversationMessage.id < cursor_message.id,
                ),
            )
        )
    message_rows = database.scalars(
        message_query.order_by(
            ConversationMessage.created_at.desc(), ConversationMessage.id.desc()
        ).limit(101)
    ).all()
    has_older = len(message_rows) > 100
    message_rows = list(reversed(message_rows[:100]))
    messages = [
        {
            "message": message,
            "sender": database.get(User, message.sender_id),
            "mine": message.sender_id == user.id,
        }
        for message in message_rows
    ]
    peer_read_at = None
    if conversation.kind == "direct" and peer:
        peer_read_at = peer["membership"].last_read_at
    events = database.scalars(
        select(ConversationEvent)
        .where(ConversationEvent.conversation_id == conversation.id)
        .order_by(ConversationEvent.created_at.desc())
        .limit(25)
    ).all()
    membership.last_read_at = datetime.now(UTC)
    database.commit()
    browser_session = current_session(request, database)
    return render(
        request,
        "conversation_detail.html",
        {
            "user": user,
            "conversation": conversation,
            "conversation_id": str(conversation.id),
            "participants": participants,
            "peer": peer,
            "blocked": blocked,
            "messages": messages,
            "has_older": has_older,
            "older_cursor": message_rows[0].id if has_older and message_rows else None,
            "peer_read_at": peer_read_at,
            "events": events,
            "csrf_token": browser_session.csrf_token,
        },
    )


@app.post("/conversations/{conversation_id}/members")
async def invite_conversation_member(
    conversation_id: UUID,
    request: Request,
    username: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    conversation = database.get(Conversation, conversation_id)
    if conversation is None or conversation.kind != "group" or conversation.created_by != user.id:
        raise HTTPException(status_code=404, detail="Group not found.")
    active_conversation_member(database, conversation.id, user.id)
    target = database.scalar(
        select(User).where(User.username == username.strip().lower(), User.is_active.is_(True))
    )
    if target is None:
        return RedirectResponse(f"/conversations/{conversation.id}?error=user-not-found", status_code=303)
    if is_blocked_between(database, user.id, target.id):
        return RedirectResponse(f"/conversations/{conversation.id}?error=blocked", status_code=303)
    membership = database.get(ConversationMember, (conversation.id, target.id))
    if membership is not None and membership.left_at is None:
        return RedirectResponse(f"/conversations/{conversation.id}?error=already-member", status_code=303)
    active_count = database.scalar(
        select(func.count(ConversationMember.user_id)).where(
            ConversationMember.conversation_id == conversation.id,
            ConversationMember.left_at.is_(None),
        )
    ) or 0
    if active_count >= 20:
        return RedirectResponse(f"/conversations/{conversation.id}?error=group-full", status_code=303)
    if membership is None:
        membership = ConversationMember(
            conversation_id=conversation.id,
            user_id=target.id,
            role="member",
        )
        database.add(membership)
    else:
        membership.left_at = None
        membership.last_read_at = None
    database.add(
        ConversationEvent(
            conversation_id=conversation.id,
            actor_user_id=user.id,
            subject_user_id=target.id,
            action="member_added",
            details=f"@{target.username} was added to the group",
        )
    )
    database.commit()
    await conversation_sockets.broadcast(
        conversation.id,
        {
            "type": "membership",
            "action": "member_added",
            "subject_user_id": str(target.id),
            "details": f"@{target.username} was added to the group",
        },
    )
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@app.post("/conversations/{conversation_id}/members/{target_id}/remove")
async def remove_conversation_member(
    conversation_id: UUID,
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    conversation = database.get(Conversation, conversation_id)
    if conversation is None or conversation.kind != "group" or conversation.created_by != user.id:
        raise HTTPException(status_code=404, detail="Group not found.")
    active_conversation_member(database, conversation.id, user.id)
    target_membership = active_conversation_member(database, conversation.id, target_id)
    if target_membership.role == "owner":
        raise HTTPException(status_code=409, detail="The group owner cannot be removed.")
    target = database.get(User, target_id)
    target_membership.left_at = datetime.now(UTC)
    database.add(
        ConversationEvent(
            conversation_id=conversation.id,
            actor_user_id=user.id,
            subject_user_id=target_id,
            action="member_removed",
            details=f"@{target.username if target else 'member'} was removed from the group",
        )
    )
    database.commit()
    await conversation_sockets.broadcast(
        conversation.id,
        {
            "type": "membership",
            "action": "member_removed",
            "subject_user_id": str(target_id),
            "details": f"@{target.username if target else 'member'} was removed from the group",
        },
    )
    await conversation_sockets.disconnect_user(conversation.id, target_id)
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@app.post("/conversations/{conversation_id}/leave")
async def leave_group_conversation(
    conversation_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    conversation = database.get(Conversation, conversation_id)
    membership = active_conversation_member(database, conversation_id, user.id)
    if conversation is None or conversation.kind != "group":
        raise HTTPException(status_code=404, detail="Group not found.")
    if membership.role == "owner":
        raise HTTPException(status_code=409, detail="The group owner cannot leave.")
    membership.left_at = datetime.now(UTC)
    database.add(
        ConversationEvent(
            conversation_id=conversation.id,
            actor_user_id=user.id,
            subject_user_id=user.id,
            action="member_left",
            details=f"@{user.username} left the group",
        )
    )
    database.commit()
    await conversation_sockets.broadcast(
        conversation.id,
        {
            "type": "membership",
            "action": "member_left",
            "subject_user_id": str(user.id),
            "details": f"@{user.username} left the group",
        },
    )
    await conversation_sockets.disconnect_user(conversation.id, user.id)
    return RedirectResponse("/conversations", status_code=303)


@app.post("/conversations/{conversation_id}/read")
def mark_conversation_read(
    conversation_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    membership = active_conversation_member(database, conversation_id, user.id)
    membership.last_read_at = datetime.now(UTC)
    database.commit()
    return {"status": "ok"}


@app.post("/conversations/{conversation_id}/messages")
async def send_conversation_message(
    conversation_id: UUID,
    request: Request,
    body: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    membership = active_conversation_member(database, conversation_id, user.id)
    conversation = database.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conversation.kind == "direct":
        recipient = database.scalar(
            select(ConversationMember.user_id).where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.user_id != user.id,
                ConversationMember.left_at.is_(None),
            )
        )
        if recipient is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if is_blocked_between(database, user.id, recipient):
            raise HTTPException(status_code=403, detail="Direct messaging is blocked between these accounts.")
    body = body.strip()
    if not body or len(body) > 5000:
        raise HTTPException(status_code=422, detail="Message must contain 1–5,000 characters.")
    if not check_rate_limit(str(user.id), f"message:{conversation.id}", limit=60, seconds=60):
        raise HTTPException(status_code=429, detail="You are sending messages too quickly.")
    message = ConversationMessage(
        conversation_id=conversation.id,
        sender_id=user.id,
        body=body,
    )
    database.add(message)
    membership.last_read_at = datetime.now(UTC)
    database.commit()
    payload = message_payload(message, user)
    await conversation_sockets.broadcast(
        conversation.id,
        {"type": "message", "message": payload},
    )
    if request.headers.get("X-Chat-Request") == "true":
        return JSONResponse(payload)
    return RedirectResponse(f"/conversations/{conversation.id}", status_code=303)


@app.websocket("/ws/conversations/{conversation_id}")
async def conversation_websocket(
    websocket: WebSocket,
    conversation_id: UUID,
    database: Session = Depends(get_db),
):
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host", "")
    if origin and origin.split("://", 1)[-1].rstrip("/").lower() != host.lower():
        await websocket.close(code=4403)
        return
    browser_session = current_session(websocket, database)
    if browser_session is None or browser_session.user_id is None:
        await websocket.close(code=4401)
        return
    member = database.get(ConversationMember, (conversation_id, browser_session.user_id))
    if member is None or member.left_at is not None:
        await websocket.close(code=4403)
        return
    await conversation_sockets.connect(conversation_id, browser_session.user_id, websocket)
    try:
        while True:
            heartbeat = await websocket.receive_text()
            if heartbeat == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        conversation_sockets.disconnect(conversation_id, websocket)


@app.post("/people/{target_id}/follow")
def follow_user(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    target = database.get(User, target_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot follow yourself.")
    if is_blocked_between(database, user.id, target.id):
        return redirect_people("A block prevents this follow.")
    edge = database.get(UserRelationship, (user.id, target.id, "follow"))
    if edge is None:
        database.add(UserRelationship(source_user_id=user.id, target_user_id=target.id, kind="follow"))
        database.commit()
    return redirect_people()


@app.post("/people/{target_id}/unfollow")
def unfollow_user(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    edge = database.get(UserRelationship, (user.id, target_id, "follow"))
    if edge is not None:
        database.delete(edge)
        database.commit()
    return redirect_people()


@app.post("/people/{target_id}/block")
def block_user(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    target = database.get(User, target_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot block yourself.")
    if database.get(UserRelationship, (user.id, target.id, "block")) is None:
        database.add(UserRelationship(source_user_id=user.id, target_user_id=target.id, kind="block"))
    follows = database.scalars(
        select(UserRelationship).where(
            UserRelationship.kind == "follow",
            or_(
                (UserRelationship.source_user_id == user.id)
                & (UserRelationship.target_user_id == target.id),
                (UserRelationship.source_user_id == target.id)
                & (UserRelationship.target_user_id == user.id),
            ),
        )
    ).all()
    for follow in follows:
        database.delete(follow)
    database.commit()
    return redirect_people()


@app.post("/people/{target_id}/unblock")
def unblock_user(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    edge = database.get(UserRelationship, (user.id, target_id, "block"))
    if edge is not None:
        database.delete(edge)
        database.commit()
    return redirect_people()


@app.get("/communities", response_class=HTMLResponse)
def communities_page(
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    visible_condition = Community.visibility == "public"
    if user is not None:
        visible_condition = or_(
            visible_condition,
            Community.id.in_(
                select(CommunityMembership.community_id).where(CommunityMembership.user_id == user.id)
            ),
        )
    communities = database.scalars(
        select(Community).where(visible_condition).order_by(Community.name).limit(100)
    ).all()
    roles = {
        community.id: community_role(database, community.id, user.id)
        for community in communities
    } if user else {}
    browser_session = current_session(request, database) if user else None
    return render(
        request,
        "communities.html",
        {
            "user": user,
            "communities": communities,
            "roles": roles,
            "csrf_token": browser_session.csrf_token if browser_session else None,
        },
    )


@app.get("/communities/new", response_class=HTMLResponse)
def new_community_page(
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    browser_session = current_session(request, database)
    return render(request, "community_new.html", form_context(browser_session, user=user))


@app.post("/communities")
def create_community(
    request: Request,
    name: str = Form(),
    slug: str = Form(),
    description: str = Form(""),
    visibility: str = Form("public"),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, browser_session = authenticated_form_user(request, database, csrf_token)
    name = name.strip()
    slug = slug.strip().lower()
    description = description.strip()
    error = None
    if not name or len(name) > 80:
        error = "Community name must be 1–80 characters."
    elif not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 48:
        error = "Use a 1–48 character slug with lowercase letters, numbers, and single hyphens."
    elif len(description) > 1000:
        error = "Description must be at most 1000 characters."
    elif visibility not in {"public", "private"}:
        error = "Choose public or private visibility."
    elif database.scalar(select(Community.id).where(Community.slug == slug)):
        error = "That community address is already in use."
    if error:
        return render(
            request,
            "community_new.html",
            form_context(browser_session, user=user, error=error, values={
                "name": name, "slug": slug, "description": description, "visibility": visibility
            }),
            status_code=422,
        )

    community = Community(
        name=name,
        slug=slug,
        description=description,
        visibility=visibility,
        created_by=user.id,
    )
    database.add(community)
    database.flush()
    database.add(CommunityMembership(community_id=community.id, user_id=user.id, role="owner"))
    database.add(
        CommunityAuditEvent(
            community_id=community.id,
            actor_user_id=user.id,
            action="community_created",
            details=f"Created {visibility} community {slug}",
        )
    )
    try:
        database.commit()
    except IntegrityError:
        database.rollback()
        return render(
            request,
            "community_new.html",
            form_context(browser_session, user=user, error="That community address is already in use."),
            status_code=422,
        )
    return RedirectResponse(f"/communities/{community.slug}", status_code=303)


@app.get("/communities/{slug}", response_class=HTMLResponse)
def community_detail(
    slug: str,
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    community = find_visible_community(database, slug, user)
    role = community_role(database, community.id, user.id) if user else None
    members = database.execute(
        select(CommunityMembership, User)
        .join(User, User.id == CommunityMembership.user_id)
        .where(CommunityMembership.community_id == community.id)
        .order_by(CommunityMembership.joined_at)
    ).all()
    events = []
    hidden_author_ids = blocked_user_ids(database, user.id) if user else set()
    post_rows = database.execute(
        select(Post, User)
        .join(User, User.id == Post.author_id)
        .where(Post.community_id == community.id, Post.removed_at.is_(None))
        .order_by(Post.created_at.desc())
        .limit(50)
    ).all()
    posts = [
        (post, author)
        for post, author in post_rows
        if author.id not in hidden_author_ids
    ]
    if role in {"owner", "moderator"}:
        events = database.scalars(
            select(CommunityAuditEvent)
            .where(CommunityAuditEvent.community_id == community.id)
            .order_by(CommunityAuditEvent.created_at.desc())
            .limit(50)
        ).all()
    browser_session = current_session(request, database) if user else None
    return render(
        request,
        "community_detail.html",
        {
            "user": user,
            "community": community,
            "role": role,
            "members": members,
            "posts": posts,
            "events": events,
            "csrf_token": browser_session.csrf_token if browser_session else None,
        },
    )


@app.post("/communities/{slug}/join")
def join_community(
    slug: str,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = database.scalar(select(Community).where(Community.slug == slug))
    if community is None:
        raise HTTPException(status_code=404, detail="Community not found.")
    if community.visibility != "public":
        raise HTTPException(status_code=404, detail="Community not found.")
    if community_role(database, community.id, user.id) is None:
        database.add(CommunityMembership(community_id=community.id, user_id=user.id, role="member"))
        database.commit()
    return RedirectResponse(f"/communities/{slug}", status_code=303)


@app.post("/communities/{slug}/leave")
def leave_community(
    slug: str,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    membership = database.get(CommunityMembership, (community.id, user.id))
    if membership is not None:
        if membership.role == "owner":
            raise HTTPException(status_code=409, detail="The owner cannot leave the community.")
        database.delete(membership)
        database.commit()
    return RedirectResponse("/communities", status_code=303)


@app.post("/communities/{slug}/members")
def invite_community_member(
    slug: str,
    request: Request,
    username: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    require_community_manager(database, community, user)
    target = database.scalar(select(User).where(User.username == username.strip().lower()))
    if target is None or not target.is_active:
        return RedirectResponse(f"/communities/{slug}?error=member-not-found", status_code=303)
    if is_blocked_between(database, user.id, target.id):
        return RedirectResponse(f"/communities/{slug}?error=blocked", status_code=303)
    if community_role(database, community.id, target.id) is None:
        database.add(
            CommunityMembership(community_id=community.id, user_id=target.id, role="member")
        )
        database.add(
            CommunityAuditEvent(
                community_id=community.id,
                actor_user_id=user.id,
                target_user_id=target.id,
                action="member_invited",
                details=f"Added @{target.username} as a member",
            )
        )
        database.commit()
    return RedirectResponse(f"/communities/{slug}", status_code=303)


@app.post("/communities/{slug}/members/{target_id}/role")
def update_community_role(
    slug: str,
    target_id: UUID,
    request: Request,
    role: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    require_community_manager(database, community, user, owner_only=True)
    if role not in {"member", "moderator"}:
        raise HTTPException(status_code=422, detail="Role must be member or moderator.")
    membership = database.get(CommunityMembership, (community.id, target_id))
    if membership is None or membership.role == "owner":
        raise HTTPException(status_code=404, detail="Member not found.")
    previous_role = membership.role
    if previous_role != role:
        membership.role = role
        database.add(
            CommunityAuditEvent(
                community_id=community.id,
                actor_user_id=user.id,
                target_user_id=membership.user_id,
                action="role_changed",
                details=f"Changed role from {previous_role} to {role}",
            )
        )
        database.commit()
    return RedirectResponse(f"/communities/{slug}", status_code=303)


@app.post("/communities/{slug}/members/{target_id}/remove")
def remove_community_member(
    slug: str,
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    actor_role = require_community_manager(database, community, user)
    membership = database.get(CommunityMembership, (community.id, target_id))
    if membership is None or membership.role == "owner":
        raise HTTPException(status_code=404, detail="Member not found.")
    if actor_role != "owner" and membership.role != "member":
        raise HTTPException(status_code=403, detail="Moderators may remove members only.")
    database.add(
        CommunityAuditEvent(
            community_id=community.id,
            actor_user_id=user.id,
            target_user_id=membership.user_id,
            action="member_removed",
            details=f"Removed @{membership.user.username} from the community",
        )
    )
    database.delete(membership)
    database.commit()
    return RedirectResponse(f"/communities/{slug}", status_code=303)


@app.post("/communities/{slug}/posts")
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
    database.commit()
    return RedirectResponse(f"/communities/{slug}/posts/{post.id}", status_code=303)


@app.get("/communities/{slug}/posts/{post_id}", response_class=HTMLResponse)
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
    comment_rows = database.execute(
        select(Comment, User)
        .join(User, User.id == Comment.author_id)
        .where(Comment.post_id == post.id)
        .order_by(Comment.created_at)
    ).all()
    nodes = {}
    hidden_comment_ids = set()
    for comment, comment_author in comment_rows:
        if comment_author.id in hidden_author_ids or (
            comment.parent_id is not None and comment.parent_id in hidden_comment_ids
        ):
            hidden_comment_ids.add(comment.id)
            continue
        nodes[comment.id] = {
            "comment": comment,
            "author": comment_author,
            "children": [],
        }
    roots = []
    for node in nodes.values():
        parent_id = node["comment"].parent_id
        if parent_id in nodes:
            nodes[parent_id]["children"].append(node)
        elif parent_id is None:
            roots.append(node)
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


@app.post("/communities/{slug}/posts/{post_id}/comments")
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


@app.post("/communities/{slug}/posts/{post_id}/remove")
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
                database, community.id, user.id, "post", post.id, "post_removed"
            )
        database.commit()
    return RedirectResponse(f"/communities/{slug}", status_code=303)


@app.post("/communities/{slug}/posts/{post_id}/lock")
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
        )
        database.commit()
    return RedirectResponse(f"/communities/{slug}/posts/{post.id}", status_code=303)


@app.post("/communities/{slug}/posts/{post_id}/comments/{comment_id}/remove")
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
            database, community.id, user.id, "comment", comment.id, "comment_removed"
        )
    database.commit()
    return RedirectResponse(f"/communities/{slug}/posts/{post.id}#comments", status_code=303)


@app.post("/communities/{slug}/reports/{target_type}/{target_id}")
def report_content(
    slug: str,
    target_type: str,
    target_id: UUID,
    request: Request,
    reason: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    if target_type == "post":
        target = find_post(database, community, target_id)
        author_id = target.author_id
        unavailable = target.removed_at is not None
    elif target_type == "comment":
        target = database.get(Comment, target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Content not found.")
        post = find_post(database, community, target.post_id)
        author_id = target.author_id
        unavailable = target.removed_at is not None or post.removed_at is not None
    else:
        raise HTTPException(status_code=404, detail="Content not found.")
    if unavailable or (user.id == author_id) or is_blocked_between(database, user.id, author_id):
        raise HTTPException(status_code=404, detail="Content not found.")
    reason = reason.strip()
    if not reason or len(reason) > 1000:
        return RedirectResponse(
            f"/communities/{slug}/posts/{target.post_id if target_type == 'comment' else target.id}?error=invalid-report",
            status_code=303,
        )
    if not allow_request(request, "report-content"):
        raise HTTPException(status_code=429, detail="Too many reports. Try again later.")
    database.add(
        ContentReport(
            community_id=community.id,
            reporter_id=user.id,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
        )
    )
    try:
        database.commit()
    except IntegrityError:
        database.rollback()
        post_id = target.post_id if target_type == "comment" else target.id
        return RedirectResponse(
            f"/communities/{slug}/posts/{post_id}?error=already-reported", status_code=303
        )
    post_id = target.post_id if target_type == "comment" else target.id
    return RedirectResponse(f"/communities/{slug}/posts/{post_id}", status_code=303)


@app.get("/communities/{slug}/reports", response_class=HTMLResponse)
def report_queue(
    slug: str,
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    community = find_visible_community(database, slug, user)
    if user is None:
        raise HTTPException(status_code=403, detail="Sign in to continue.")
    require_community_manager(database, community, user)
    report_rows = database.scalars(
        select(ContentReport)
        .where(ContentReport.community_id == community.id, ContentReport.status == "open")
        .order_by(ContentReport.created_at)
        .limit(100)
    ).all()
    reports = []
    for report in report_rows:
        target = database.get(Post if report.target_type == "post" else Comment, report.target_id)
        if target is None:
            continue
        if report.target_type == "comment":
            post = database.get(Post, target.post_id)
            author = database.get(User, target.author_id)
        else:
            post = target
            author = database.get(User, target.author_id)
        reporter = database.get(User, report.reporter_id)
        reports.append(
            {
                "report": report,
                "target": target,
                "post": post,
                "author": author,
                "reporter": reporter,
            }
        )
    browser_session = current_session(request, database)
    return render(
        request,
        "report_queue.html",
        {
            "user": user,
            "community": community,
            "reports": reports,
            "csrf_token": browser_session.csrf_token,
        },
    )


@app.post("/communities/{slug}/report-queue/{report_id}/resolve")
def resolve_report(
    slug: str,
    report_id: UUID,
    request: Request,
    decision: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    require_community_manager(database, community, user)
    report = database.scalar(
        select(ContentReport)
        .where(
            ContentReport.id == report_id,
            ContentReport.community_id == community.id,
            ContentReport.status == "open",
        )
        .with_for_update()
    )
    if report is None:
        raise HTTPException(status_code=404, detail="Open report not found.")
    if decision not in {"remove", "dismiss"}:
        raise HTTPException(status_code=422, detail="Choose remove or dismiss.")
    if decision == "remove":
        target = database.get(Post if report.target_type == "post" else Comment, report.target_id)
        if target is not None and target.removed_at is None:
            target.removed_at = datetime.now(UTC)
            target.removed_by_user_id = user.id
            add_moderation_event(
                database,
                community.id,
                user.id,
                report.target_type,
                target.id,
                f"{report.target_type}_removed",
                report.reason,
            )
        report.status = "resolved"
    else:
        report.status = "dismissed"
    report.resolved_by_user_id = user.id
    report.resolved_at = datetime.now(UTC)
    add_moderation_event(
        database,
        community.id,
        user.id,
        "report",
        report.id,
        f"report_{report.status}",
        report.reason,
    )
    reporter = database.get(User, report.reporter_id)
    if reporter is not None:
        outcome = "removed" if decision == "remove" else "dismissed"
        queue_notification(
            database,
            reporter,
            user,
            "moderation",
            f"A moderator reviewed your report: {outcome}.",
        )
    database.commit()
    return RedirectResponse(f"/communities/{slug}/reports", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    database: Session = Depends(get_db),
):
    if authenticated_user(request, database):
        return RedirectResponse("/profile", status_code=303)
    browser_session = ensure_browser_session(request, database)
    return render(request, "login.html", form_context(browser_session))


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    browser_session = current_session(request, database)
    if browser_session is None:
        raise HTTPException(status_code=403, detail="Reload the form and try again.")
    require_csrf(request, browser_session, csrf_token)
    if not allow_request(request, "login"):
        return invalid_form(request, "login.html", browser_session, "Too many attempts. Try again later.")
    user = database.scalar(select(User).where(User.email == email.strip().lower()))
    password_hash = user.password_hash if user else DUMMY_PASSWORD_HASH
    password_matches = verify_password(password, password_hash)
    if user is None or not user.is_active or not password_matches:
        return invalid_form(request, "login.html", browser_session, "Email or password is incorrect.")
    return begin_authenticated_session(database, user, request)


@app.post("/logout")
def logout(
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    browser_session = current_session(request, database)
    if browser_session is None:
        return RedirectResponse("/", status_code=303)
    require_csrf(request, browser_session, csrf_token)
    database.delete(browser_session)
    database.commit()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(session_cookie_name(), path="/")
    return response


@app.get("/profile", response_class=HTMLResponse)
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


@app.post("/profile", response_class=HTMLResponse)
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


@app.get("/settings", response_class=HTMLResponse)
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


@app.post("/settings")
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


@app.get("/account/export")
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
            or_(UserRelationship.source_user_id == user.id, UserRelationship.target_user_id == user.id)
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


@app.post("/account/delete")
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

    database.query(CommunityMembership).filter(
        CommunityMembership.user_id == user.id
    ).delete(synchronize_session=False)
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


@app.get("/account-deleted", response_class=HTMLResponse)
def account_deleted_page(request: Request):
    return render(request, "account_deleted.html", {"user": None})


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(
    request: Request,
    database: Session = Depends(get_db),
):
    browser_session = ensure_browser_session(request, database)
    return render(request, "forgot_password.html", form_context(browser_session))


@app.post("/forgot-password", response_class=HTMLResponse)
def forgot_password(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    browser_session = current_session(request, database)
    if browser_session is None:
        raise HTTPException(status_code=403, detail="Reload the form and try again.")
    require_csrf(request, browser_session, csrf_token)
    if not allow_request(request, "forgot-password"):
        return render(
            request,
            "forgot_password.html",
            form_context(browser_session, error="Too many attempts. Try again later."),
            status_code=429,
        )
    user = database.scalar(select(User).where(User.email == email.strip().lower()))
    if user and user.is_active:
        raw_token = new_token()
        database.query(PasswordResetToken).filter(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        ).update({PasswordResetToken.used_at: datetime.now(UTC)})
        database.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=token_digest(raw_token),
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )
        database.commit()
        reset_url = f"{get_settings().app_base_url}/reset-password?token={quote(raw_token)}"
        background_tasks.add_task(send_password_reset_email, user.email, reset_url)
    return render(
        request,
        "forgot_password.html",
        form_context(browser_session, sent=True),
    )


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(
    request: Request,
    token: str,
    database: Session = Depends(get_db),
):
    browser_session = ensure_browser_session(request, database)
    reset_record = database.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_digest(token),
            PasswordResetToken.used_at.is_(None),
        )
    )
    if reset_record is None or _is_expired(reset_record.expires_at):
        return render(
            request,
            "reset_password.html",
            form_context(browser_session, token=token, error="This reset link is invalid or expired."),
            status_code=400,
        )
    return render(request, "reset_password.html", form_context(browser_session, token=token))


def _is_expired(expiry: datetime) -> bool:
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry <= datetime.now(UTC)


@app.post("/reset-password", response_class=HTMLResponse)
def reset_password(
    request: Request,
    token: str = Form(),
    password: str = Form(),
    password_confirmation: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    browser_session = current_session(request, database)
    if browser_session is None:
        raise HTTPException(status_code=403, detail="Reload the form and try again.")
    require_csrf(request, browser_session, csrf_token)
    reset_record = database.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_digest(token),
            PasswordResetToken.used_at.is_(None),
        ).with_for_update()
    )
    if reset_record is None or _is_expired(reset_record.expires_at):
        return render(
            request,
            "reset_password.html",
            form_context(browser_session, token=token, error="This reset link is invalid or expired."),
            status_code=400,
        )
    if len(password) < 12 or len(password.encode("utf-8")) > 128:
        return render(
            request,
            "reset_password.html",
            form_context(browser_session, token=token, error="Use a password between 12 and 128 bytes."),
            status_code=422,
        )
    if password != password_confirmation:
        return render(
            request,
            "reset_password.html",
            form_context(browser_session, token=token, error="Passwords do not match."),
            status_code=422,
        )
    user = database.get(User, reset_record.user_id)
    if user is None or not user.is_active:
        return render(
            request,
            "reset_password.html",
            form_context(browser_session, token=token, error="This reset link is invalid or expired."),
            status_code=400,
        )
    user.password_hash = hash_password(password)
    reset_record.used_at = datetime.now(UTC)
    database.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None),
    ).update({PasswordResetToken.used_at: datetime.now(UTC)})
    database.query(UserSession).filter(UserSession.user_id == user.id).delete()
    database.commit()
    response = RedirectResponse("/login?password_reset=1", status_code=303)
    response.delete_cookie(session_cookie_name(), path="/")
    return response
