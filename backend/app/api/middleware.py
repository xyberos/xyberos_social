"""HTTP middleware and the unhandled-error handler."""

import logging
import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.api.deps import client_key
from app.core.config import get_settings
from app.core.metrics import http_request_duration, http_requests, route_label, status_class
from app.core.observability import REQUEST_ID_HEADER, request_id_var, resolve_request_id
from app.core.scoping import ScopeViolation

logger = logging.getLogger(__name__)


_APP_CSP = (
    "default-src 'self'; script-src 'self' https://unpkg.com; "
    "style-src 'self' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; object-src 'none'; "
    # connect-src covers the same-origin chat WebSocket.
    "connect-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)

# FastAPI's generated docs pull Swagger UI and ReDoc from jsDelivr and bootstrap them with an
# inline script, so they cannot render under _APP_CSP.
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
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        _DOCS_CSP if request.url.path in _DOCS_PATHS else _APP_CSP
    )
    if get_settings().app_env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# Probes and scrapes run on a fixed interval, so they log at DEBUG instead of flooding the
# request log with entries nobody reads.
_QUIET_PATHS = {"/healthz", "/readyz", "/metrics"}


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


def register_middleware(app: FastAPI) -> None:
    """Register middleware so the request context wraps the security headers, then the 500 handler."""
    app.middleware("http")(add_security_headers)
    app.middleware("http")(add_request_context)
    app.add_exception_handler(ScopeViolation, scope_violation_response)
    app.add_exception_handler(Exception, unhandled_error_response)
