"""HTTP middleware and the unhandled-error handler."""

import logging
import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.api.deps import client_key
from app.core.config import get_settings
from app.core.observability import REQUEST_ID_HEADER, request_id_var, resolve_request_id

logger = logging.getLogger(__name__)


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
        # connect-src covers the same-origin chat WebSocket.
        "connect-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    if get_settings().app_env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


_HEALTH_PATHS = {"/healthz", "/readyz"}


def log_request(request: Request, request_id: str, status_code: int, started: float) -> None:
    path = request.url.path
    if path.startswith("/static/"):
        return
    logger.log(
        logging.DEBUG if path in _HEALTH_PATHS else logging.INFO,
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


async def add_request_context(request: Request, call_next):
    request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
    request.state.request_id = request_id
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        log_request(request, request_id, response.status_code, started)
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


def register_middleware(app: FastAPI) -> None:
    """Register middleware so the request context wraps the security headers, then the 500 handler."""
    app.middleware("http")(add_security_headers)
    app.middleware("http")(add_request_context)
    app.add_exception_handler(Exception, unhandled_error_response)
