"""The mobile shell: what the head promises and what the tab bar offers.

These are the pieces a browser cannot verify without a device: that the viewport meta enables
the safe-area insets the tab bar relies on, that the app can be installed, and that the tab bar
contains the destinations a phone actually needs while exactly one of them is current.
"""

import json

from conftest import create_account


def signed_in(client_factory):
    client = client_factory()
    assert create_account(client, "mobile@example.com", "mobile").status_code == 303
    return client


def test_the_viewport_allows_safe_areas_and_a_resizing_keyboard(client):
    head = client.get("/").text

    assert (
        'content="width=device-width, initial-scale=1, viewport-fit=cover, '
        'interactive-widget=resizes-content"' in head
    )
    # One per scheme, so the browser chrome follows the theme instead of framing a green page
    # in white.
    assert 'name="theme-color" content="#245b48" media="(prefers-color-scheme: light)"' in head
    assert 'name="theme-color" content="#151b18" media="(prefers-color-scheme: dark)"' in head


def test_the_application_is_installable(client):
    head = client.get("/").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in head
    assert 'rel="apple-touch-icon"' in head

    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    document = json.loads(response.text)
    assert document["start_url"] == "/dashboard"
    assert document["display"] == "standalone"
    # A maskable icon is what keeps the mark whole once a platform applies its own mask.
    purposes = {icon["purpose"] for icon in document["icons"]}
    assert purposes == {"any", "maskable"}
    for icon in document["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]
    assert client.get("/static/icons/apple-touch-icon.png").status_code == 200


def test_the_tab_bar_offers_five_destinations_with_one_current(client_factory):
    client = signed_in(client_factory)

    page = client.get("/dashboard").text
    bar = page.split('class="mobile-tabbar"', 1)[1].split("</nav>", 1)[0]

    for href in (
        '/dashboard"',
        '/conversations"',
        '/posts/new"',
        '/communities"',
        '/people"',
    ):
        assert f'href="{href}' in bar, href
    # Alerts lives in the top bar, so the bell has to be reachable at every width.
    assert 'href="/notifications"' not in bar
    assert 'aria-label="Notifications"' in page

    # Exactly one destination is current, and it is the page being read.
    assert bar.count('aria-current="page"') == 1
    assert bar.index('href="/dashboard"') < bar.index('aria-current="page"')

    # Create is the primary act, so it is the tab that carries an icon disc.
    assert "tab-create" in bar


def test_the_top_bar_keeps_the_route_to_notifications_on_a_phone():
    css = open("backend/app/static/css/styles.css", encoding="utf-8").read()

    # The chat icon is desktop-only because the tab bar carries it; the bell is not.
    assert ".topbar-actions .topbar-icon-wide { display: none; }" in css
    assert ".topbar-actions { display: flex;" in css


def test_touch_targets_and_motion_are_considered():
    css = open("backend/app/static/css/styles.css", encoding="utf-8").read()

    assert "env(safe-area-inset-bottom)" in css
    assert "env(safe-area-inset-left)" in css
    assert "touch-action: manipulation" in css
    # A coarse pointer gets a full-size target for the text buttons, which is where the
    # destructive actions live.
    assert "@media (pointer: coarse)" in css
    # The chat composer stays above the keyboard rather than scrolling behind it.
    assert "position: sticky" in css
