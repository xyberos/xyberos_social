"""The schema guard: does the database hold what this code expects?

Deploying ahead of a migration is the failure that hides longest — the deploy reports healthy and
the first request that touches a new column fails. These tests hold the two halves of the guard:
the head is read from the migration files (so it cannot drift from them), and only production
refuses to start.
"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import app.main as main

from app.core.schema import (
    ABSENT,
    BEHIND,
    CURRENT,
    UNREACHABLE,
    SchemaNotCurrent,
    check_schema,
    head_revision,
    verify_schema,
)


def sqlite_session():
    engine = create_engine("sqlite://")
    return engine


def record(session: Session, revision: str) -> None:
    session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    session.execute(
        text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
        {"revision": revision},
    )
    session.commit()


def test_the_head_is_the_revision_nothing_else_revises():
    expected = head_revision()

    assert expected, "the migration directory should have exactly one head"
    # The head is the newest file in this repository; if it changes, this line is the reminder
    # that the guard follows the files rather than a hard-coded list.
    assert expected >= "0025_post_reactions"


def test_a_current_database_passes():
    engine = sqlite_session()
    with Session(engine) as session:
        record(session, head_revision())

        assert check_schema(session)[0] == CURRENT
        # Strict mode is silent when there is nothing to report.
        assert verify_schema(session, strict=True) == CURRENT
    engine.dispose()


def test_a_database_behind_the_code_is_reported_and_refused():
    engine = sqlite_session()
    with Session(engine) as session:
        record(session, "0021_user_avatar")

        status, applied = check_schema(session)
        assert (status, applied) == (BEHIND, "0021_user_avatar")

        # Outside production it is a warning, so a local checkout still starts.
        assert verify_schema(session, strict=False) == BEHIND
        # In production it refuses, and says what to run.
        with pytest.raises(SchemaNotCurrent, match="alembic upgrade head"):
            verify_schema(session, strict=True)
    engine.dispose()


def test_a_database_with_no_history_is_reported():
    engine = sqlite_session()
    with Session(engine) as session:
        status, applied = check_schema(session)

        assert (status, applied) == (ABSENT, None)
        with pytest.raises(SchemaNotCurrent):
            verify_schema(session, strict=True)
    engine.dispose()


def test_an_unreachable_database_never_refuses_to_start():
    """A database that is down must not turn a restart into a longer outage."""
    engine = create_engine("sqlite:////nonexistent-directory/schema.db")
    with Session(engine) as session:
        status, applied = check_schema(session)

        assert (status, applied) == (UNREACHABLE, None)
        # Strict, and still no exception: readiness reports the outage, not the boot.
        assert verify_schema(session, strict=True) == UNREACHABLE
    engine.dispose()


def test_a_failed_read_rolls_back_before_probing():
    """PostgreSQL aborts the transaction on a failed statement.

    Without the rollback, the reachability probe fails for the same reason the first query did,
    and a database that was simply never migrated is misreported as unreachable — which would
    let production start against an empty schema. This asserts the rollback happens, since
    SQLite does not reproduce the abort on its own.
    """

    class RecordingSession:
        def __init__(self):
            self.calls: list[str] = []

        def execute(self, statement):
            text_of = str(statement)
            self.calls.append(text_of)
            raise __import__("sqlalchemy").exc.OperationalError(text_of, {}, Exception("boom"))

        def rollback(self):
            self.calls.append("rollback")

    session = RecordingSession()
    status, applied = check_schema(session)

    assert (status, applied) == (UNREACHABLE, None)
    # Rolled back between the two attempts, not after them.
    assert session.calls[1] == "rollback"


def test_production_refuses_to_start_when_the_schema_is_behind(monkeypatch):
    engine = sqlite_session()
    with Session(engine) as session:
        record(session, "0001_identity")

    async def run() -> None:
        await main.verify_database_schema(
            SimpleNamespace(app_env="production", require_current_schema=True)
        )

    monkeypatch.setattr(main, "engine", engine)
    with pytest.raises(SchemaNotCurrent):
        asyncio.run(run())
    engine.dispose()


def test_an_operator_can_start_anyway(monkeypatch):
    """A restore may need to start before migrating; the flag makes that a warning."""
    engine = sqlite_session()
    with Session(engine) as session:
        record(session, "0001_identity")

    async def run() -> None:
        await main.verify_database_schema(
            SimpleNamespace(app_env="production", require_current_schema=False)
        )

    monkeypatch.setattr(main, "engine", engine)
    asyncio.run(run())
    engine.dispose()


def test_development_only_warns(monkeypatch):
    engine = sqlite_session()

    async def run() -> None:
        await main.verify_database_schema(
            SimpleNamespace(app_env="development", require_current_schema=True)
        )

    monkeypatch.setattr(main, "engine", engine)
    asyncio.run(run())
    engine.dispose()
