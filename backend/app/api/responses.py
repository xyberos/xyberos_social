"""Template rendering and redirect helpers shared by the route modules."""

from pathlib import Path
from urllib.parse import quote
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from app.api.deps import form_context, set_session_cookie
from app.models import UserSession

BASE_DIR = Path(__file__).resolve().parents[1]


templates = Jinja2Templates(directory=BASE_DIR / "templates")


def render(request: Request, name: str, context: dict, status_code: int = 200):
    response = templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
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
