"""What the application does at startup, and what it deliberately leaves to an operator."""

import asyncio
import logging
import time
from types import SimpleNamespace

import app.main as main

from app.core.storage import StorageError


def run_lifespan() -> None:
    async def run() -> None:
        async with main.lifespan(main.app):
            pass

    asyncio.run(run())


def test_development_startup_prepares_the_bucket(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(main, "bootstrap_bucket", lambda: calls.append("bootstrap") or [])
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(app_env="development"))

    run_lifespan()

    assert calls == ["bootstrap"]


def test_production_startup_leaves_the_bucket_to_the_operator(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(main, "bootstrap_bucket", lambda: calls.append("bootstrap") or [])
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(app_env="production"))

    run_lifespan()

    assert calls == []


def test_an_unreachable_storage_service_does_not_stop_startup(monkeypatch):
    def unavailable() -> list[str]:
        raise StorageError("could not reach object storage")

    monkeypatch.setattr(main, "bootstrap_bucket", unavailable)
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(app_env="development"))

    # The application still serves: the failure surfaces on the first upload instead.
    run_lifespan()


def test_a_hanging_storage_service_only_delays_startup(monkeypatch, caplog):
    def hangs() -> list[str]:
        time.sleep(0.4)
        return []

    monkeypatch.setattr(main, "bootstrap_bucket", hangs)
    monkeypatch.setattr(main, "BOOTSTRAP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(app_env="development"))

    with caplog.at_level(logging.WARNING):
        run_lifespan()

    assert any(
        "could not prepare the development bucket" in record.message
        for record in caplog.records
    )
