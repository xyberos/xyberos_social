from urllib.parse import parse_qs, urlparse

import app.api.routes.identity as identity_routes

from conftest import csrf_from


def register(client, email="ada@example.com", username="ada"):
    page = client.get("/register")
    csrf_token = csrf_from(page.text)
    response = client.post(
        "/register",
        data={
            "csrf_token": csrf_token,
            "email": email,
            "username": username,
            "display_name": "Ada Lovelace",
            "password": "correct-horse-battery-staple",
            "password_confirmation": "correct-horse-battery-staple",
        },
        follow_redirects=False,
    )
    return response


def test_register_creates_account_and_starts_session(client):
    response = register(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/conversations"
    assert "httponly" in response.headers["set-cookie"].lower()
    inbox = client.get("/conversations")
    assert inbox.status_code == 200
    assert "Your inbox is quiet" in inbox.text
    profile = client.get("/profile")
    assert profile.status_code == 200
    assert "@ada" in profile.text
    assert 'href="/settings"' in profile.text
    assert "Save notification settings" not in profile.text


def test_profile_update_requires_auth_and_saves_with_htmx(client):
    assert client.get("/profile", follow_redirects=False).headers["location"] == "/login"
    register(client)
    page = client.get("/profile")
    response = client.post(
        "/profile",
        data={
            "csrf_token": csrf_from(page.text),
            "display_name": "Ada L.",
            "bio": "Building things carefully.",
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "Profile saved." in response.text
    profile = client.get("/profile")
    assert "Ada L." in profile.text
    assert "Building things carefully." in profile.text


def test_login_rejects_invalid_csrf(client):
    response = client.post(
        "/login",
        data={"csrf_token": "not-valid", "email": "a@example.com", "password": "anything"},
    )
    assert response.status_code == 403


def test_login_and_logout(client):
    register(client)
    client.post("/logout", data={"csrf_token": csrf_from(client.get("/profile").text)})
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf_from(page.text),
            "email": "ada@example.com",
            "password": "correct-horse-battery-staple",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/profile").status_code == 200


def test_password_reset_is_generic_and_one_time(client, monkeypatch):
    delivered = []
    monkeypatch.setattr(
        identity_routes, "send_password_reset_email", lambda email, url: delivered.append(url)
    )
    register(client)
    page = client.get("/forgot-password")
    csrf_token = csrf_from(page.text)
    known = client.post(
        "/forgot-password",
        data={"csrf_token": csrf_token, "email": "ada@example.com"},
    )
    assert "If an account matches" in known.text
    assert len(delivered) == 1

    unknown = client.post(
        "/forgot-password",
        data={"csrf_token": csrf_token, "email": "nobody@example.com"},
    )
    assert "If an account matches" in unknown.text
    assert len(delivered) == 1

    token = parse_qs(urlparse(delivered[0]).query)["token"][0]
    reset_page = client.get(f"/reset-password?token={token}")
    response = client.post(
        "/reset-password",
        data={
            "csrf_token": csrf_from(reset_page.text),
            "token": token,
            "password": "a-new-correct-horse-password",
            "password_confirmation": "a-new-correct-horse-password",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    login_page = client.get("/login")
    login_response = client.post(
        "/login",
        data={
            "csrf_token": csrf_from(login_page.text),
            "email": "ada@example.com",
            "password": "a-new-correct-horse-password",
        },
        follow_redirects=False,
    )
    assert login_response.status_code == 303
    replay = client.get(f"/reset-password?token={token}")
    assert replay.status_code == 400


def test_email_verification_routes_are_disabled(client):
    assert client.get("/check-email").status_code == 404
    assert client.get("/verify-email?token=unused").status_code == 404
    assert client.get("/resend-verification").status_code == 404
