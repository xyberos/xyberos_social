"""Template rendering and redirect helpers shared by the route modules."""

from pathlib import Path
from urllib.parse import quote
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from app.api.deps import form_context, set_session_cookie, theme_from_cookie
from app.core.theme import THEME_OPTIONS, THEME_CHOICES, option_for, resolve_theme
from app.core.appearance import BACKGROUND_OPTIONS, DENSITY_OPTIONS, PRESETS
from app.core.seo import DEFAULT_DESCRIPTION, SITE_NAME, canonical_url, describe, site_url
from app.models import UserSession
from app.services.messaging import unread_message_total

BASE_DIR = Path(__file__).resolve().parents[1]


templates = Jinja2Templates(directory=BASE_DIR / "templates")

# A page writes its summary once; the helper bounds it to a snippet and collapses the
# whitespace a multi-line body would otherwise put inside an attribute.
templates.env.globals["describe"] = describe


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


def _appearance_context(context: dict) -> dict:
    """Expose only server-curated appearance choices to templates."""
    user = context.get("user")
    return {
        "appearance_preset": getattr(user, "appearance_preset", "forest"),
        "background_style": getattr(user, "background_style", "paper"),
        "display_density": getattr(user, "display_density", "comfortable"),
        "appearance_presets": PRESETS,
        "background_options": BACKGROUND_OPTIONS,
        "density_options": DENSITY_OPTIONS,
    }


def render(request: Request, name: str, context: dict, status_code: int = 200):
    response = templates.TemplateResponse(
        request=request,
        name=name,
        context={
            # Crawl metadata every page needs and none should have to restate. A template's
            # `description` block, or an explicit context value, overrides these defaults.
            "site_name": SITE_NAME,
            "site_url": site_url(),
            "canonical_url": canonical_url(request),
            "seo_description": DEFAULT_DESCRIPTION,
            # The address a form on this page returns to, so a control in a list can say where
            # it came from without every template reaching for `request` itself.
            "current_url": f"{request.url.path}?{request.url.query}"
            if request.url.query
            else request.url.path,
            **context,
            **_shell_context(request, context),
            **_theme_context(request, context),
            **_appearance_context(context),
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
