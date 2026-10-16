"""Registration, sign-in, sign-out, and password recovery."""

import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import (
    allow_request,
    authenticated_user,
    begin_authenticated_session,
    current_session,
    ensure_browser_session,
    form_context,
    require_csrf,
    session_cookie_name,
)
from app.api.responses import invalid_form, render
from app.core.config import get_settings
from app.core.database import get_db
from app.core.mail import send_password_reset_email
from app.models import PasswordResetToken, User, UserSession
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    new_token,
    token_digest,
    verify_password,
)

router = APIRouter()

logger = logging.getLogger(__name__)


USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,24}$")


@router.get("/register", response_class=HTMLResponse)
def register_page(
    request: Request,
    database: Session = Depends(get_db),
):
    if authenticated_user(request, database):
        return RedirectResponse("/dashboard", status_code=303)
    browser_session = ensure_browser_session(request, database)
    return render(request, "auth/register.html", form_context(browser_session))


@router.post("/register", response_class=HTMLResponse)
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
        return invalid_form(
            request, "auth/register.html", browser_session, "Too many attempts. Try again later."
        )

    email = email.strip().lower()
    username = username.strip().lower()
    display_name = display_name.strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 320:
        return invalid_form(
            request, "auth/register.html", browser_session, "Enter a valid email address."
        )
    if not USERNAME_PATTERN.fullmatch(username):
        return invalid_form(
            request,
            "auth/register.html",
            browser_session,
            "Username must be 3–24 characters using letters, numbers, or underscores.",
        )
    if not display_name or len(display_name) > 80:
        return invalid_form(
            request, "auth/register.html", browser_session, "Display name must be 1–80 characters."
        )
    if len(password) < 8 or len(password.encode("utf-8")) > 128:
        return invalid_form(
            request, "auth/register.html", browser_session, "Use a password between 8 and 128 bytes."
        )
    if password != password_confirmation:
        return invalid_form(request, "auth/register.html", browser_session, "Passwords do not match.")
    if database.scalar(select(User.id).where((User.email == email) | (User.username == username))):
        return invalid_form(
            request,
            "auth/register.html",
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
        return invalid_form(
            request, "auth/register.html", browser_session, "Could not create the account. Try again."
        )
    return begin_authenticated_session(database, user, request)


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    database: Session = Depends(get_db),
):
    if authenticated_user(request, database):
        return RedirectResponse("/dashboard", status_code=303)
    browser_session = ensure_browser_session(request, database)
    return render(request, "auth/login.html", form_context(browser_session))


@router.post("/login", response_class=HTMLResponse)
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
        return invalid_form(
            request, "auth/login.html", browser_session, "Too many attempts. Try again later."
        )
    user = database.scalar(select(User).where(User.email == email.strip().lower()))
    password_hash = user.password_hash if user else DUMMY_PASSWORD_HASH
    password_matches = verify_password(password, password_hash)
    if user is None or not user.is_active or not password_matches:
        return invalid_form(
            request, "auth/login.html", browser_session, "Email or password is incorrect."
        )
    return begin_authenticated_session(database, user, request)


@router.post("/logout")
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


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(
    request: Request,
    database: Session = Depends(get_db),
):
    browser_session = ensure_browser_session(request, database)
    return render(request, "auth/forgot_password.html", form_context(browser_session))


@router.post("/forgot-password", response_class=HTMLResponse)
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
            "auth/forgot_password.html",
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
        "auth/forgot_password.html",
        form_context(browser_session, sent=True),
    )


@router.get("/reset-password", response_class=HTMLResponse)
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
            "auth/reset_password.html",
            form_context(
                browser_session, token=token, error="This reset link is invalid or expired."
            ),
            status_code=400,
        )
    return render(request, "auth/reset_password.html", form_context(browser_session, token=token))


def _is_expired(expiry: datetime) -> bool:
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry <= datetime.now(UTC)


@router.post("/reset-password", response_class=HTMLResponse)
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
        select(PasswordResetToken)
        .where(
            PasswordResetToken.token_hash == token_digest(token),
            PasswordResetToken.used_at.is_(None),
        )
        .with_for_update()
    )
    if reset_record is None or _is_expired(reset_record.expires_at):
        return render(
            request,
            "auth/reset_password.html",
            form_context(
                browser_session, token=token, error="This reset link is invalid or expired."
            ),
            status_code=400,
        )
    if len(password) < 12 or len(password.encode("utf-8")) > 128:
        return render(
            request,
            "auth/reset_password.html",
            form_context(
                browser_session, token=token, error="Use a password between 12 and 128 bytes."
            ),
            status_code=422,
        )
    if password != password_confirmation:
        return render(
            request,
            "auth/reset_password.html",
            form_context(browser_session, token=token, error="Passwords do not match."),
            status_code=422,
        )
    user = database.get(User, reset_record.user_id)
    if user is None or not user.is_active:
        return render(
            request,
            "auth/reset_password.html",
            form_context(
                browser_session, token=token, error="This reset link is invalid or expired."
            ),
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
