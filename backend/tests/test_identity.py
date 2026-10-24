import re
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

import app.api.routes.identity as identity_routes
import app.api.routes.account as account_routes

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
    assert response.headers["location"] == "/dashboard"
    assert "httponly" in response.headers["set-cookie"].lower()
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert 'href="/posts/new"' in dashboard.text
    inbox = client.get("/conversations")
    assert inbox.status_code == 200
    assert "Your inbox is quiet" in inbox.text
    profile = client.get("/profile")
    assert profile.status_code == 200
    assert "@ada" in profile.text
    assert 'href="/settings"' in profile.text
    assert "Save notification settings" not in profile.text


class AvatarStorage:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, payload, *, content_type):
        self.objects[key] = (payload, content_type)

    def get_bytes(self, key):
        return self.objects[key][0]

    def delete(self, key):
        self.objects.pop(key, None)


def avatar_storage(monkeypatch):
    storage = AvatarStorage()
    monkeypatch.setattr(account_routes, "get_storage", lambda: storage)
    return storage


def png_bytes(color="purple", size=(4, 4)):
    image = BytesIO()
    Image.new("RGB", size, color).save(image, format="PNG")
    return image.getvalue()


def upload_avatar(client, payload, filename="avatar.png", content_type="image/png"):
    page = client.get("/profile")
    return client.post(
        "/profile/avatar",
        data={"csrf_token": csrf_from(page.text)},
        files={"avatar": (filename, payload, content_type)},
        follow_redirects=False,
    )


def avatar_url_from(client):
    return re.search(r'<img src="(/avatars/[^"]+)"', client.get("/profile").text).group(1)


def test_avatar_upload_is_sanitized_served_replaced_and_can_fall_back_to_an_initial(
    client, monkeypatch
):
    storage = avatar_storage(monkeypatch)
    register(client)
    uploaded = upload_avatar(client, png_bytes())
    assert uploaded.headers["location"] == "/profile?avatar_saved=1"
    assert '<img src="/avatars/' in client.get("/profile").text
    avatar_url = avatar_url_from(client)
    assert "?v=" in avatar_url
    response = client.get(avatar_url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=300"

    replaced = upload_avatar(client, png_bytes("orange"))
    assert replaced.headers["location"] == "/profile?avatar_saved=1"
    replaced_url = avatar_url_from(client)
    assert replaced_url != avatar_url
    # Only the current object is retained: the superseded one is reclaimed after the commit.
    assert len(storage.objects) == 1

    removed = client.post(
        "/profile/avatar/remove",
        data={"csrf_token": csrf_from(client.get("/profile").text)},
        follow_redirects=False,
    )
    assert removed.headers["location"] == "/profile?avatar_removed=1"
    assert '<img src="/avatars/' not in client.get("/profile").text
    assert storage.objects == {}


def test_avatar_upload_rejects_bytes_that_are_not_an_image(client, monkeypatch):
    storage = avatar_storage(monkeypatch)
    register(client)
    rejected = upload_avatar(
        client, b"not an image", filename="avatar.png", content_type="image/png"
    )
    assert rejected.headers["location"] == "/profile?avatar_error=image"
    assert storage.objects == {}


def test_avatar_upload_applies_exif_orientation(client, monkeypatch):
    avatar_storage(monkeypatch)
    register(client)
    image = Image.new("RGB", (6, 2), "purple")
    exif = Image.Exif()
    exif[0x0112] = 6  # The phone recorded the frame rotated 90° clockwise.
    payload = BytesIO()
    image.save(payload, format="JPEG", exif=exif)
    uploaded = upload_avatar(
        client, payload.getvalue(), filename="portrait.jpg", content_type="image/jpeg"
    )
    assert uploaded.headers["location"] == "/profile?avatar_saved=1"
    served = client.get(avatar_url_from(client))
    with Image.open(BytesIO(served.content)) as stored:
        assert stored.size == (2, 6)


def test_account_deletion_removes_the_avatar_object(client, monkeypatch):
    storage = avatar_storage(monkeypatch)
    register(client)
    upload_avatar(client, png_bytes())
    assert len(storage.objects) == 1

    page = client.get("/profile")
    deleted = client.post(
        "/account/delete",
        data={
            "csrf_token": csrf_from(page.text),
            "password": "correct-horse-battery-staple",
            "confirmation": "DELETE",
        },
        follow_redirects=False,
    )
    assert deleted.headers["location"] == "/account-deleted"
    assert storage.objects == {}


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


def menu_logout_token(page: str) -> str:
    """The token from the account menu's logout form, which the shell renders on every page."""
    menu = page.split('class="account-menu"', 1)
    assert len(menu) == 2, "the page has no account menu"
    match = re.search(
        r'action="/logout">\s*<input type="hidden" name="csrf_token" value="([^"]+)"', menu[1]
    )
    assert match, "the account menu has no logout form with a token"
    return match.group(1)


def test_the_account_menu_offers_profile_and_a_working_logout(client):
    """The menu lives in the shell, so every signed-in page must render its token."""
    register(client)
    for path in (
        "/dashboard",
        "/workspace",
        "/profile",
        "/settings",
        "/notifications",
        "/people",
        "/communities",
        "/conversations",
        "/posts/new",
    ):
        page = client.get(path)
        assert page.status_code == 200, path
        # The menu opts out of the boosted header: these navigations replace the whole shell,
        # and a #main-content swap would leave the open menu and the rail behind on logout.
        assert '<details class="account-menu" hx-boost="false">' in page.text, path
        menu = page.text.split('class="account-menu"', 1)[1]
        assert 'href="/profile"' in menu, path
        assert 'href="/settings"' in menu, path
        menu_logout_token(page.text)

    # The dashboard has no form of its own, so logging out from its menu proves the shell's
    # form is wired everywhere the menu appears.
    dashboard = client.get("/dashboard")
    logged_out = client.post(
        "/logout", data={"csrf_token": menu_logout_token(dashboard.text)}, follow_redirects=False
    )
    assert logged_out.status_code == 303
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/login"


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


def change_password(client, *, current, new, confirmation=None):
    return client.post(
        "/account/password",
        data={
            "csrf_token": csrf_from(client.get("/settings").text),
            "current_password": current,
            "new_password": new,
            "new_password_confirmation": new if confirmation is None else confirmation,
        },
        follow_redirects=False,
    )


def test_changing_the_password_replaces_the_credential(client):
    register(client)
    page = client.get("/settings").text
    assert 'action="/account/password"' in page
    assert 'name="current_password"' in page
    assert 'name="new_password"' in page
    assert 'name="new_password_confirmation"' in page

    response = change_password(
        client, current="correct-horse-battery-staple", new="a-much-longer-passphrase"
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?password=1"
    assert "Password updated" in client.get("/settings?password=1").text

    client.post("/logout", data={"csrf_token": csrf_from(client.get("/settings").text)})
    page = client.get("/login")
    stale = client.post(
        "/login",
        data={
            "csrf_token": csrf_from(page.text),
            "email": "ada@example.com",
            "password": "correct-horse-battery-staple",
        },
        follow_redirects=False,
    )
    assert stale.status_code == 422
    assert "Email or password is incorrect" in stale.text

    page = client.get("/login")
    fresh = client.post(
        "/login",
        data={
            "csrf_token": csrf_from(page.text),
            "email": "ada@example.com",
            "password": "a-much-longer-passphrase",
        },
        follow_redirects=False,
    )
    assert fresh.status_code == 303
    assert client.get("/settings").status_code == 200


def test_a_password_change_requires_the_current_password(client):
    register(client)
    response = change_password(
        client, current="not-the-password", new="a-much-longer-passphrase"
    )
    assert response.status_code == 403
    assert "current password is incorrect" in response.text
    # The session survives a refused attempt, and the old credential is still the one in use.
    assert client.get("/settings").status_code == 200


@pytest.mark.parametrize(
    ("new", "confirmation", "message"),
    [
        ("short-pass", None, "between 12 and 128"),
        ("a-much-longer-passphrase", "a-different-long-passphrase", "do not match"),
        ("correct-horse-battery-staple", None, "differs from the current one"),
    ],
)
def test_a_new_password_is_validated(client, new, confirmation, message):
    register(client)
    response = change_password(
        client,
        current="correct-horse-battery-staple",
        new=new,
        confirmation=confirmation,
    )
    assert response.status_code == 422
    assert message in response.text


def test_the_current_session_survives_and_other_devices_do_not(client_factory):
    first = client_factory()
    register(first)
    second = client_factory()
    page = second.get("/login")
    assert (
        second.post(
            "/login",
            data={
                "csrf_token": csrf_from(page.text),
                "email": "ada@example.com",
                "password": "correct-horse-battery-staple",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert second.get("/settings").status_code == 200

    assert (
        change_password(
            first, current="correct-horse-battery-staple", new="a-much-longer-passphrase"
        ).status_code
        == 303
    )

    assert first.get("/settings").status_code == 200
    signed_out = second.get("/settings", follow_redirects=False)
    assert signed_out.status_code == 303
    assert signed_out.headers["location"] == "/login"


def test_a_password_change_retires_an_unused_reset_link(client, monkeypatch):
    delivered = []
    monkeypatch.setattr(
        identity_routes, "send_password_reset_email", lambda email, url: delivered.append(url)
    )
    register(client)
    page = client.get("/forgot-password")
    client.post(
        "/forgot-password",
        data={"csrf_token": csrf_from(page.text), "email": "ada@example.com"},
    )
    token = parse_qs(urlparse(delivered[0]).query)["token"][0]

    assert (
        change_password(
            client, current="correct-horse-battery-staple", new="a-much-longer-passphrase"
        ).status_code
        == 303
    )

    retired = client.get(f"/reset-password?token={token}")
    assert retired.status_code == 400
    assert "invalid or expired" in retired.text


def test_email_verification_routes_are_disabled(client):
    assert client.get("/check-email").status_code == 404
    assert client.get("/verify-email?token=unused").status_code == 404
    assert client.get("/resend-verification").status_code == 404
