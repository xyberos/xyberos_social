"""Guarded rehearsal of the full migration chain against a scratch PostgreSQL database."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

MIGRATION_URL_ENV = "SOCCOM_MIGRATION_TEST_DATABASE_URL"
ALEMBIC_URL_ENV = "ALEMBIC_DATABASE_URL"
BACKEND_DIR = Path(__file__).resolve().parents[1]

APPLICATION_TABLES = {
    "users",
    "user_sessions",
    "communities",
    "community_memberships",
    "posts",
    "comments",
    "conversations",
    "conversation_messages",
    "calls",
    "call_participants",
    "notifications",
    "platform_audit_events",
    "outbox_events",
    "idempotency_keys",
    "attachments",
}


def migration_database_url() -> str:
    url = os.environ.get(MIGRATION_URL_ENV)
    if not url:
        pytest.skip(
            f"Set {MIGRATION_URL_ENV} to a scratch PostgreSQL database to rehearse migrations."
        )
    database_name = make_url(url).database or ""
    if "test" not in database_name.lower():
        pytest.fail(
            f"Refusing to rehearse migrations against {database_name!r}: "
            "this test drops every application table, so the database name must contain 'test'."
        )
    return url


def alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return config


def table_names(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_migrations_upgrade_downgrade_upgrade(monkeypatch):
    url = migration_database_url()
    monkeypatch.setenv(ALEMBIC_URL_ENV, url)
    config = alembic_config()

    command.upgrade(config, "head")
    upgraded = table_names(url)
    assert "alembic_version" in upgraded
    assert APPLICATION_TABLES <= upgraded, APPLICATION_TABLES - upgraded

    command.downgrade(config, "base")
    downgraded = table_names(url)
    assert not (APPLICATION_TABLES & downgraded), APPLICATION_TABLES & downgraded

    command.upgrade(config, "head")
    reupgraded = table_names(url)
    assert APPLICATION_TABLES <= reupgraded, APPLICATION_TABLES - reupgraded
