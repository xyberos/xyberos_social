"""The metrics surface: bounded labels, accurate counters, and a scrape that degrades well."""

import asyncio

import pytest
from fastapi import Request
from sqlalchemy.exc import OperationalError

from conftest import create_account, csrf_from

from app.api.middleware import add_request_context
from app.core import metrics


@pytest.fixture(autouse=True)
def clean_metrics():
    metrics.registry.reset()
    yield
    metrics.registry.reset()


def scrape(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    return response.text


def gauge_value(body: str, name: str) -> float | None:
    for line in body.splitlines():
        if line.startswith(name + " "):
            return float(line.rsplit(" ", 1)[1])
    return None


def test_request_counters_and_latency_are_exported(client):
    client.get("/healthz")

    body = scrape(client)

    assert "# TYPE soccom_http_requests_total counter" in body
    assert "# TYPE soccom_http_request_duration_seconds histogram" in body
    assert 'soccom_http_requests_total{method="GET",route="/healthz",status="2xx"} 1' in body
    assert 'soccom_http_request_duration_seconds_count{method="GET",route="/healthz"} 1' in body
    assert 'soccom_http_request_duration_seconds_bucket{le="+Inf",method="GET",route="/healthz"} 1' in body
    assert gauge_value(body, "soccom_uptime_seconds") is not None
    # The test engine pools statically and reports nothing; the metric is still declared.
    assert "# TYPE soccom_db_pool_connections gauge" in body


def test_pool_gauges_report_queue_pool_states(client, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import QueuePool

    from app.api.routes import health

    monkeypatch.setattr(health, "engine", create_engine("sqlite://", poolclass=QueuePool))

    body = scrape(client)

    for state in ("size", "checked_in", "checked_out", "overflow"):
        assert f'soccom_db_pool_connections{{state="{state}"}}' in body


def test_path_parameters_are_substituted_out_of_route_labels(client):
    assert create_account(client, "metrics@example.com", "metricsuser").status_code == 303
    page = client.get("/communities/new")
    created = client.post(
        "/communities",
        data={
            "csrf_token": csrf_from(page.text),
            "name": "Metrics Lab",
            "slug": "metrics-lab",
            "description": "",
            "visibility": "public",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert client.get("/communities/metrics-lab").status_code == 200

    body = scrape(client)

    assert 'route="/communities/{slug}"' in body
    assert 'route="/communities"' in body
    # The concrete value must never be a label, or one series would exist per community.
    assert "metrics-lab" not in body


def test_unmatched_requests_share_one_placeholder(client):
    assert client.get("/no-such-page-12345").status_code == 404

    body = scrape(client)

    assert 'soccom_http_requests_total{method="GET",route="unmatched",status="4xx"} 1' in body
    assert "no-such-page-12345" not in body


def test_unhandled_failures_are_counted_as_server_errors():
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
            "state": {},
        }
    )

    async def failing(_request):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(add_request_context(request, failing))

    assert metrics.http_requests.value(route="unmatched", method="GET", status="5xx") == 1


def test_queue_gauges_reflect_stored_state(client):
    assert create_account(client, "queues@example.com", "queuesuser").status_code == 303
    page = client.get("/communities/new")
    assert (
        client.post(
            "/communities",
            data={
                "csrf_token": csrf_from(page.text),
                "name": "Queue Lab",
                "slug": "queue-lab",
                "description": "",
                "visibility": "public",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )

    body = scrape(client)

    # Creating a community enqueues one event, and nothing publishes it yet.
    assert gauge_value(body, "soccom_outbox_unpublished_events") == 1
    assert gauge_value(body, "soccom_idempotency_keys_in_progress") == 0


def test_scrape_survives_a_database_failure(client):
    class BrokenSession:
        def scalar(self, *_args, **_kwargs):
            raise OperationalError("SELECT 1", None, Exception("connection refused"))

    from app.core.database import get_db
    from app.main import app

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: BrokenSession()
    try:
        body = scrape(client)
    finally:
        if previous is not None:
            app.dependency_overrides[get_db] = previous
        else:
            app.dependency_overrides.pop(get_db, None)

    assert gauge_value(body, "soccom_uptime_seconds") is not None
    assert "soccom_outbox_unpublished_events" not in body
