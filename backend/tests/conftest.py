import itertools

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app

_test_client_counter = itertools.count(1)


@pytest.fixture
def client_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    def override_get_db():
        database = TestingSession()
        try:
            yield database
        finally:
            database.close()

    app.dependency_overrides[get_db] = override_get_db
    test_clients = []

    def create_client():
        client_number = next(_test_client_counter)
        test_client = TestClient(
            app,
            client=(f"198.18.{client_number // 254}.{client_number % 254 + 1}", 50000),
        )
        test_clients.append(test_client)
        return test_client

    yield create_client
    for test_client in test_clients:
        test_client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(client_factory):
    return client_factory()


def csrf_from(page: str) -> str:
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, "CSRF token missing from rendered form"
    return match.group(1)


def create_account(client, email: str, username: str):
    page = client.get("/register")
    return client.post(
        "/register",
        data={
            "csrf_token": csrf_from(page.text),
            "email": email,
            "username": username,
            "display_name": username.title(),
            "password": "correct-horse-battery-staple",
            "password_confirmation": "correct-horse-battery-staple",
        },
        follow_redirects=False,
    )
