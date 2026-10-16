import asyncio
import json
import logging

import pytest
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

from conftest import create_account


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


def test_a_redirect_on_an_htmx_request_becomes_a_full_navigation(client):
    """A boosted link swaps one piece of the page, so it cannot render a different shell.

    This is the expired-session path: the page a signed-in visitor is looking at belongs to a
    session the server no longer knows, and the answer has to replace that whole page.
    """
    plain = client.get("/dashboard", follow_redirects=False)
    assert plain.status_code == 303
    assert plain.headers["location"] == "/login"

    boosted = client.get("/dashboard", headers={"HX-Request": "true"}, follow_redirects=False)
    assert boosted.status_code == 200
    assert boosted.headers["hx-redirect"] == "/login"
    assert boosted.text == ""
    # The conversion replaces the body, not the response: its other headers still reach the
    # client, request id included.
    assert boosted.headers["x-request-id"]
    assert "content-type" not in boosted.headers


def test_an_htmx_request_that_resolves_keeps_its_normal_answer(client_factory):
    signed_in = client_factory()
    assert create_account(signed_in, "ada@example.com", "ada").status_code == 303

    response = signed_in.get("/dashboard", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "hx-redirect" not in response.headers
    assert 'id="main-content"' in response.text


@pytest.mark.parametrize("path", ["/docs", "/docs/oauth2-redirect", "/redoc"])
def test_docs_pages_get_a_csp_that_allows_the_swagger_assets(client, path):
    response = client.get(path)

    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in policy
    assert "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in policy


def test_application_routes_keep_the_strict_csp(client):
    policy = client.get("/healthz").headers["content-security-policy"]

    assert "script-src 'self' https://unpkg.com" in policy
    assert "https://cdn.jsdelivr.net" not in policy
    assert "'unsafe-inline'" not in policy


def test_static_assets_revalidate_instead_of_being_cached_heuristically(client):
    response = client.get("/static/css/styles.css")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_csp_allows_the_configured_storage_origin(client, monkeypatch):
    """A presigned upload posts bytes straight to storage, so the browser needs that origin."""
    monkeypatch.setattr(
        "app.api.middleware.get_settings",
        lambda: Settings(s3_endpoint_url="https://objects.example.com"),
    )

    policy = client.get("/healthz").headers["content-security-policy"]

    assert "connect-src 'self' https://objects.example.com;" in policy
    # Only the configured origin is added, and only to connect-src.
    assert "script-src 'self' https://unpkg.com" in policy


def test_csp_omits_a_storage_origin_when_unset(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.middleware.get_settings", lambda: Settings(s3_endpoint_url=None)
    )

    policy = client.get("/healthz").headers["content-security-policy"]

    assert "connect-src 'self';" in policy


def test_hsts_only_when_running_in_production(client, monkeypatch):
    production = Settings(
        app_env="production",
        cookie_secure=True,
        smtp_host="smtp.example",
        smtp_starttls=True,
        app_base_url="https://soccom.example",
        s3_endpoint_url="https://objects.example",
        clamav_host="clamav.example",
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
