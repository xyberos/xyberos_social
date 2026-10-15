"""Request correlation and structured logging."""

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import uuid4

REQUEST_ID_HEADER = "X-Request-ID"
_ACCEPTED_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_STRUCTURED_FIELDS = (
    "event",
    "request_id",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "client",
)

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str:
    return request_id_var.get() or "-"


def resolve_request_id(candidate: str | None) -> str:
    """Reuse an upstream request id only when it is short and inert, otherwise mint one."""
    if candidate and _ACCEPTED_REQUEST_ID.match(candidate):
        return candidate
    return uuid4().hex


def _structured_fields(record: logging.LogRecord) -> dict[str, object]:
    fields: dict[str, object] = {}
    for name in _STRUCTURED_FIELDS:
        value = getattr(record, name, None)
        if value is not None:
            fields[name] = value
    return fields


class RequestIdFilter(logging.Filter):
    """Attaches the active request id so no call site has to pass it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "request_id", None) is None:
            record.request_id = current_request_id()
        return True


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        fields = _structured_fields(record)
        if not fields:
            return message
        return message + " " + " ".join(f"{name}={value}" for name, value in fields.items())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_structured_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(json_logs: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter()
        if json_logs
        else TextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Every request is logged by the request-context middleware, so uvicorn's access log
    # would only duplicate it.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
