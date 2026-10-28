"""HTTP middleware and the unhandled-error handler."""

import logging
import time
from functools import lru_cache
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import client_key
from app.api.responses import render
from app.core.assets import is_current_version
from app.core.config import get_settings
from app.core.metrics import http_request_duration, http_requests, route_label, status_class
from app.core.observability import REQUEST_ID_HEADER, request_id_var, resolve_request_id
from app.core.scoping import ScopeViolation

logger = logging.getLogger(__name__)


def _storage_origin(endpoint_url: str | None) -> str | None:
    """The one extra origin a browser may connect to: the configured object storage."""
    if not endpoint_url:
        return None
    parsed = urlparse(endpoint_url)
    if not parsed.scheme or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


@lru_cache(maxsize=8)
def _app_csp(endpoint_url: str | None) -> str:
    """The application policy, plus the storage origin a presigned upload posts bytes to.

    Without that origin in `connect-src` the browser refuses the direct upload, which is the
    whole point of issuing a presigned target instead of proxying attachment bytes through the
    application. Only the configured origin is added.
    """
    connect_sources = ["'self'"]
    origin = _storage_origin(endpoint_url)
    if origin:
        connect_sources.append(origin)
    return (
        # HTMX is still the one third-party script; the fonts are served from this origin, so
        # `font-src` and `style-src` no longer name a Google host.
        "default-src 'self'; script-src 'self' https://unpkg.com; "
        "style-src 'self'; "
        # blob: lets a delivered attachment render from the reader's own local copy after the
        # stored object is released.
        "font-src 'self'; img-src 'self' data: blob:; "
        # A post's bytes are served from this origin; blob: covers the composer's preview of
        # a file that is still uploading, and the chat's local copy of a received one.
        "media-src 'self' blob:; object-src 'none'; "
        # connect-src covers the same-origin chat WebSocket and the attachment upload target.
        f"connect-src {' '.join(connect_sources)}; base-uri 'self'; "
        "frame-ancestors 'none'; form-action 'self'"
    )


# FastAPI's generated docs pull Swagger UI and ReDoc from jsDelivr and bootstrap them with an
# inline script, so they cannot render under the application policy.
_DOCS_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc"}
_DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; object-src 'none'; "
    "connect-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)


async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # A page that needs a device permission sets its own deliberate policy; everything else
    # gets the restrictive one. Only the call surface asks for the microphone.
    if "Permissions-Policy" not in response.headers:
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if _is_private_path(request.url.path):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    # A route may set its own deliberate policy (attachment delivery serves stored media under
    # a narrower one); everything else gets the application policy for its route class.
    if "Content-Security-Policy" not in response.headers:
        settings = get_settings()
        response.headers["Content-Security-Policy"] = (
            _DOCS_CSP
            if request.url.path in _DOCS_PATHS
            else _app_csp(settings.s3_endpoint_url)
        )
    if get_settings().app_env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # An asset address carries the digest of its bytes, so a reader who names the current version
    # can keep it indefinitely: a deploy changes the address, not the cache entry. A request for
    # any other version — or none, which is what a direct visit sends — revalidates instead, so a
    # stale address can never pin old CSS into a browser.
    if request.url.path.startswith("/static/"):
        # The helper keys on the path under `static/`, not the request path, which carries the
        # mount prefix.
        relative = request.url.path[len("/static/") :]
        if is_current_version(relative, request.query_params.get("v")):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
    return response


# A redirect on an HTMX request becomes an HX-Redirect, so the browser navigates instead of
# swapping a fragment of the answer into the page the previous session rendered.
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def htmx_navigation(request: Request, response: Response) -> Response:
    """Turn a redirect on an HTMX request into the full page load HTMX cannot perform itself.

    A boosted link or form follows its redirect with a second request and swaps only the piece
    it was told to (`#main-content`), which leaves the shell around it showing the previous
    session: a signed-in top bar and rail wrapped around the sign-in page. That is exactly the
    case where the server said "not here" (most often an expired session), so the browser has
    to navigate and the answer is an `HX-Redirect` header instead of a redirect status.
    """
    if request.headers.get("HX-Request") != "true":
        return response
    if response.status_code not in _REDIRECT_STATUSES:
        return response
    location = response.headers.get("location")
    if not location:
        return response
    # Headers other than the target still belong to the client, so only the ones this body
    # would have carried are dropped.
    headers = {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in {"location", "content-length", "content-type"}
    }
    headers["HX-Redirect"] = location
    return Response(status_code=200, headers=headers)


# Probes and scrapes run on a fixed interval, so they log at DEBUG instead of flooding the
# request log with entries nobody reads.
_QUIET_PATHS = {"/healthz", "/readyz", "/metrics"}

# Everything behind the sign-in wall. A crawler that reaches one of these is told, in the answer
# itself, that the page is not a search result: the 303 to `/login` says only where to go next,
# and a search engine is free to read that as "this address now lives at the sign-in page".
_PRIVATE_PREFIXES = (
    "/dashboard",
    "/updates",
    "/conversations",
    "/notifications",
    "/people",
    "/account",
    "/calls",
    "/attachments",
    "/posts/new",
    "/communities/new",
    "/moderation",
)


def _is_private_path(path: str) -> bool:
    """A moderation queue is the one walled page that hangs off a public prefix."""
    return path.startswith(_PRIVATE_PREFIXES) or path.endswith("/reports")


def log_request(request: Request, request_id: str, status_code: int, started: float) -> None:
    path = request.url.path
    if path.startswith("/static/"):
        return
    logger.log(
        logging.DEBUG if path in _QUIET_PATHS else logging.INFO,
        "request completed",
        extra={
            "event": "request",
            "method": request.method,
            "path": path,
            "status_code": status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "client": client_key(request),
            "request_id": request_id,
        },
    )


def record_request_metrics(request: Request, status_code: int, elapsed: float) -> None:
    labels = {"route": route_label(request), "method": request.method}
    http_requests.inc(status=status_class(status_code), **labels)
    http_request_duration.observe(elapsed, **labels)


async def add_request_context(request: Request, call_next):
    request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
    request.state.request_id = request_id
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        try:
            response = await call_next(request)
        except Exception:
            # The catch-all error handler is the outermost middleware, so an unhandled
            # failure never reaches the code below and its 500 is counted here instead.
            record_request_metrics(request, 500, time.perf_counter() - started)
            raise
        response = htmx_navigation(request, response)
        response.headers[REQUEST_ID_HEADER] = request_id
        log_request(request, request_id, response.status_code, started)
        record_request_metrics(request, response.status_code, time.perf_counter() - started)
        return response
    finally:
        request_id_var.reset(token)


async def unhandled_error_response(request: Request, exc: Exception) -> JSONResponse:
    """Registered so an unhandled failure still returns a correlatable request id."""
    request_id = getattr(request.state, "request_id", None) or "-"
    logger.exception(
        "unhandled error",
        extra={"event": "request.failed", "path": request.url.path, "request_id": request_id},
    )
    return JSONResponse(
        {"detail": "Internal server error.", "request_id": request_id},
        status_code=500,
        headers={REQUEST_ID_HEADER: request_id},
    )


async def scope_violation_response(request: Request, exc: Exception) -> JSONResponse:
    """A row outside the caller's scope answers exactly like a row that does not exist."""
    request_id = getattr(request.state, "request_id", None) or "-"
    logger.warning(
        "scope violation",
        extra={"event": "request.scope_denied", "path": request.url.path, "request_id": request_id},
    )
    return JSONResponse(
        {"detail": "Not found.", "request_id": request_id},
        status_code=404,
        headers={REQUEST_ID_HEADER: request_id},
    )


async def not_found_response(request: Request, exc: Exception) -> Response:
    """Answer a missing page in the application shell when a browser asked for a page.

    FastAPI's JSON body is right for a caller that parses it and wrong for everyone else: a
    reader gets a raw object instead of a page, and a crawler is told this address is not an
    HTML document. A navigation keeps the shell and the 404 status; anything else keeps JSON.
    """
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", "Not found.")
    request_id = getattr(request.state, "request_id", None) or "-"
    headers = dict(getattr(exc, "headers", None) or {})
    headers[REQUEST_ID_HEADER] = request_id
    is_page_navigation = (
        status_code == 404
        and request.method in {"GET", "HEAD"}
        and "text/html" in request.headers.get("accept", "")
    )
    if not is_page_navigation:
        return JSONResponse(
            {"detail": detail, "request_id": request_id},
            status_code=status_code,
            headers=headers,
        )
    response = render(request, "errors/404.html", {"user": None}, status_code=404)
    response.headers.update(headers)
    return response


def register_middleware(app: FastAPI) -> None:
    """Register middleware so the request context wraps the security headers, then the 500 handler."""
    app.middleware("http")(add_security_headers)
    app.middleware("http")(add_request_context)
    app.add_exception_handler(StarletteHTTPException, not_found_response)
    app.add_exception_handler(ScopeViolation, scope_violation_response)
    app.add_exception_handler(Exception, unhandled_error_response)
