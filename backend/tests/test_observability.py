import asyncio
import json
import logging

from fastapi import Request
from sqlalchemy.exc import OperationalError

from app.api.middleware import unhandled_error_response
from app.core.config import Settings
from app.core.database import get_db
from app.core.observability import (
    JsonFormatter,
    RequestIdFilter,
    request_id_var,
    resolve_request_id,
)
from app.main import app


def test_request_id_is_generated_and_returned(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    assert request_id == resolve_request_id(request_id)


def test_upstream_request_id_is_reused(client):
    response = client.get("/healthz", headers={"X-Request-ID": "trace-12345678"})

    assert response.headers["x-request-id"] == "trace-12345678"


def test_unsafe_request_id_is_replaced(client):
    response = client.get("/healthz", headers={"X-Request-ID": "not a valid request id"})

    assert response.headers["x-request-id"] != "not a valid request id"
    assert len(response.headers["x-request-id"]) == 32


def test_security_headers_cover_framing_and_connections(client):
    response = client.get("/healthz")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "connect-src 'self'" in response.headers["content-security-policy"]
    assert "strict-transport-security" not in response.headers


def test_hsts_only_when_running_in_production(client, monkeypatch):
    production = Settings(
        app_env="production",
        cookie_secure=True,
        smtp_host="smtp.example",
        smtp_starttls=True,
        app_base_url="https://soccom.example",
        database_url="postgresql+psycopg://soccom:secret@localhost:5432/soccom",
    )
    monkeypatch.setattr("app.api.middleware.get_settings", lambda: production)

    response = client.get("/healthz")

    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_readyz_reports_unavailable_when_the_database_fails(client):
    class BrokenSession:
        def execute(self, *_args, **_kwargs):
            raise OperationalError("SELECT 1", None, Exception("connection refused"))

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: BrokenSession()
    try:
        response = client.get("/readyz")
    finally:
        if previous is not None:
            app.dependency_overrides[get_db] = previous
        else:
            app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_unhandled_errors_return_a_correlatable_request_id():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/boom",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("127.0.0.1", 1234),
            "state": {"request_id": "trace-abcdef12"},
        }
    )

    response = asyncio.run(unhandled_error_response(request, RuntimeError("boom")))

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "trace-abcdef12"
    assert json.loads(response.body) == {
        "detail": "Internal server error.",
        "request_id": "trace-abcdef12",
    }


def test_json_formatter_emits_structured_fields():
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request completed",
        args=(),
        exc_info=None,
    )
    record.request_id = "trace-abcdef12"
    record.status_code = 200
    record.duration_ms = 4.5

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["message"] == "request completed"
    assert payload["request_id"] == "trace-abcdef12"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 4.5


def test_request_id_filter_falls_back_to_the_active_context():
    token = request_id_var.set("trace-inflight1")
    try:
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="inside a request",
            args=(),
            exc_info=None,
        )
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "trace-inflight1"
    finally:
        request_id_var.reset(token)
