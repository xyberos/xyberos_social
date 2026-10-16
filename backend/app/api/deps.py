"""Request-scoped dependencies: browser sessions, CSRF, authentication, and rate limits."""

import secrets
from datetime import UTC, datetime
from fastapi import HTTPException, Request, Response, WebSocket
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.models import User, UserSession
from app.core.security import check_rate_limit, new_token, session_expiry, token_digest

SESSION_COOKIE = "soccom_session"


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


def begin_authenticated_session(
    database: Session, user: User, request: Request
) -> RedirectResponse:
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
    response = RedirectResponse("/dashboard", status_code=303)
    set_session_cookie(response, raw_token)
    return response


def authenticated_form_user(
    request: Request, database: Session, csrf_token: str
) -> tuple[User, UserSession]:
    browser_session = current_session(request, database)
    user = authenticated_user(request, database)
    if browser_session is None or user is None:
        raise HTTPException(status_code=403, detail="Sign in to continue.")
    require_csrf(request, browser_session, csrf_token)
    return user, browser_session
