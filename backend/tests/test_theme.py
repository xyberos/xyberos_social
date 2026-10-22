"""Theme selection: the device cookie, the account setting, and the document attribute.

The attribute is what the stylesheet keys off, so these tests assert the rendered `<html>`
rather than looking for a class name: "system" must leave `data-theme` off so the media query
can follow the operating system.
"""

import re

from sqlalchemy import select

from conftest import create_account, csrf_from

from app.models import User

SETTINGS_FORM = {
    "updates_source": "all",
    "updates_days": "7",
    "updates_limit": "20",
}


def theme_attribute(page: str) -> str | None:
    match = re.search(r'<html[^>]*\bdata-theme="([^"]+)"', page)
    return match.group(1) if match else None


def theme_choice(page: str) -> str | None:
    match = re.search(r'<html[^>]*\bdata-theme-choice="([^"]+)"', page)
    return match.group(1) if match else None


def test_a_guest_choice_is_remembered_for_the_device(client):
    page = client.get("/login")
    # "System" is the absence of an explicit theme, so the CSS media query decides.
    assert theme_attribute(page.text) is None
    assert theme_choice(page.text) == "system"
    assert 'aria-label="Theme: System"' in page.text
    assert 'content="light dark"' in page.text

    response = client.post(
        "/theme", data={"theme": "dark", "next": "/login"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "soccom_theme=dark" in response.headers["set-cookie"]

    page = client.get("/login")
    assert theme_attribute(page.text) == "dark"
    assert theme_choice(page.text) == "dark"
    assert 'aria-label="Theme: Dark"' in page.text
    assert 'content="dark"' in page.text


def test_the_switch_stays_on_the_site(client):
    response = client.post(
        "/theme",
        data={"theme": "light", "next": "https://evil.example/phish"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_an_unknown_theme_is_refused(client):
    response = client.post("/theme", data={"theme": "sepia"}, follow_redirects=False)
    assert response.status_code == 422
    assert "soccom_theme" not in response.headers.get("set-cookie", "")


def test_every_page_offers_the_three_choices(client_factory):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    for path in ("/dashboard", "/settings", "/conversations", "/login"):
        page = alice.get(path).text
        assert 'class="theme-menu"' in page, path
        for value in ("system", "light", "dark"):
            assert f'data-theme-value="{value}"' in page, path


def test_the_account_choice_wins_and_is_saved(client_factory, database_factory):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303

    # The header switch updates the account and the device cookie in one request.
    switched = alice.post(
        "/theme",
        data={"theme": "dark"},
        headers={"X-Theme-Update": "true"},
        follow_redirects=False,
    )
    assert switched.status_code == 204
    assert "soccom_theme=dark" in switched.headers["set-cookie"]
    with database_factory() as database:
        assert database.scalar(select(User.theme).where(User.username == "alice")) == "dark"

    # The settings page offers the same choice, and saving it wins over the cookie.
    page = alice.get("/settings")
    assert theme_attribute(page.text) == "dark"
    saved = alice.post(
        "/settings",
        data={"csrf_token": csrf_from(page.text), **SETTINGS_FORM, "theme": "light"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert "soccom_theme=light" in saved.headers["set-cookie"]
    with database_factory() as database:
        assert database.scalar(select(User.theme).where(User.username == "alice")) == "light"

    assert theme_attribute(alice.get("/dashboard").text) == "light"
    assert theme_choice(alice.get("/dashboard").text) == "light"


def test_the_settings_form_refuses_an_unknown_theme(client_factory):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    page = alice.get("/settings")
    response = alice.post(
        "/settings",
        data={"csrf_token": csrf_from(page.text), **SETTINGS_FORM, "theme": "sepia"},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Choose valid settings." in response.text
