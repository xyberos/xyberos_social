"""Template rendering and redirect helpers shared by the route modules."""

from pathlib import Path
from urllib.parse import quote
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from app.api.deps import form_context, set_session_cookie, theme_from_cookie
from app.core.theme import THEME_OPTIONS, THEME_CHOICES, option_for, resolve_theme
from app.models import UserSession
from app.services.messaging import unread_message_total

BASE_DIR = Path(__file__).resolve().parents[1]


templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _shell_context(request: Request, context: dict) -> dict:
    """Counts the shell shows on every page, so the side rail never disagrees with the inbox."""
    user = context.get("user")
    database = getattr(request.state, "database", None)
    if user is None or database is None:
        return {}
    try:
        return {"chat_unread_total": unread_message_total(database, user_id=user.id)}
    except SQLAlchemyError:
        # The page still renders; a shell badge is not worth failing a request over.
        return {"chat_unread_total": 0}


def _theme_context(request: Request, context: dict) -> dict:
    """The account's choice wins when signed in; a guest gets this device's last choice."""
    user = context.get("user")
    choice = getattr(user, "theme", None) if user is not None else None
    if choice not in THEME_CHOICES:
        choice = theme_from_cookie(request)
    choice, effective = resolve_theme(choice)
    return {
        "theme_choice": choice,
        "theme_effective": effective,
        "theme_options": THEME_OPTIONS,
        "theme_current": option_for(choice),
    }


def render(request: Request, name: str, context: dict, status_code: int = 200):
    response = templates.TemplateResponse(
        request=request,
        name=name,
        context={
            **context,
            **_shell_context(request, context),
            **_theme_context(request, context),
        },
        status_code=status_code,
    )
    pending_token = getattr(request.state, "pending_session_token", None)
    if pending_token:
        set_session_cookie(response, pending_token)
        request.state.pending_session_token = None
    return response


def invalid_form(request: Request, page: str, browser_session: UserSession, error: str, **context):
    return render(
        request,
        page,
        form_context(browser_session, error=error, **context),
        status_code=422,
    )


def redirect_people(
    message: str | None = None, next_url: str | None = None
) -> RedirectResponse:
    location = relative_target(next_url) or "/people"
    if message:
        separator = "&" if "?" in location else "?"
        location = f"{location}?{separator}message={quote(message)}"
    return RedirectResponse(location, status_code=303)


def relative_target(value: str | None) -> str | None:
    """Accept only a site-relative path, so a caller's `next` cannot leave the application."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return None
