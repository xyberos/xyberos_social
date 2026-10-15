"""Accessibility checks that a browser would otherwise have to catch by hand.

Each page is parsed and asserted against the handful of rules that a template change can
easily break: a document language, a label for every control, alternative text for every
image, an accessible name for every button, and no positive tabindex.
"""

from html.parser import HTMLParser

import pytest

from conftest import create_account, csrf_from

VOID_TYPES = {"hidden", "submit", "button", "image", "reset"}


class PageAudit(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lang: str | None = None
        self.controls: list[dict] = []
        self.label_targets: set[str] = set()
        self.images: list[dict] = []
        self.buttons: list[dict] = []
        self.positive_tabindex: list[str] = []
        self._label_depth = 0
        self._in_button = False
        self._button_text = ""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        tabindex = attributes.get("tabindex", "")
        if tabindex.lstrip("+-").isdigit() and int(tabindex) > 0:
            self.positive_tabindex.append(f"<{tag} tabindex={tabindex}>")

        if tag == "html":
            self.lang = attributes.get("lang")
        elif tag in {"input", "textarea", "select"}:
            self.controls.append({**attributes, "tag": tag, "wrapped": self._label_depth > 0})
        elif tag == "label":
            self._label_depth += 1
            if attributes.get("for"):
                self.label_targets.add(attributes["for"])
        elif tag == "img":
            self.images.append(attributes)
        elif tag == "button":
            self._in_button = True
            self._button_text = ""
            self.buttons.append(attributes)
        elif tag == "label" and attributes.get("for"):
            self.label_targets.add(attributes["for"])

    def handle_endtag(self, tag):
        if tag == "label" and self._label_depth:
            self._label_depth -= 1
        elif tag == "button" and self._in_button:
            self._in_button = False
            self.buttons[-1]["text"] = self._button_text.strip()

    def handle_data(self, data):
        if self._in_button:
            self._button_text += data


def audit(client, path: str) -> PageAudit:
    response = client.get(path)
    assert response.status_code == 200, f"{path} answered {response.status_code}"
    page = PageAudit()
    page.feed(response.text)
    return page


def assert_page_is_usable(page: PageAudit, path: str) -> None:
    assert page.lang, f"{path} has no document language"
    assert not page.positive_tabindex, f"{path} sets a positive tabindex: {page.positive_tabindex}"

    for control in page.controls:
        if control.get("type") in VOID_TYPES:
            continue
        named = bool(
            control.get("aria-label")
            or control.get("aria-labelledby")
            or control.get("title")
            or control["wrapped"]
        )
        control_id = control.get("id")
        named = named or bool(control_id and control_id in page.label_targets)
        assert named, f"{path} has an unlabelled {control['tag']} named {control.get('name')!r}"

    for image in page.images:
        assert "alt" in image, f"{path} has an image without alt text"

    for button in page.buttons:
        named = bool(button.get("aria-label") or button.get("title") or button.get("text"))
        assert named, f"{path} has a button with no accessible name"


def test_the_audit_detects_a_broken_page():
    """Guards the checker itself, so a passing audit means the pages are actually clean."""
    page = PageAudit()
    page.feed(
        '<html lang="en"><body>'
        '<img src="/logo.png">'
        '<form><input name="body"><button><span aria-hidden="true">x</span></button></form>'
        "</body></html>"
    )

    with pytest.raises(AssertionError):
        assert_page_is_usable(page, "/synthetic")


def test_anonymous_pages_are_usable(client):
    for path in ("/", "/login", "/register", "/forgot-password", "/account-deleted"):
        assert_page_is_usable(audit(client, path), path)


def test_signed_in_pages_are_usable(client_factory):
    alice = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303

    for path in (
        "/people",
        "/communities",
        "/updates",
        "/notifications",
        "/settings",
        "/profile",
        "/communities/new",
        "/conversations",
        "/conversations/new",
    ):
        assert_page_is_usable(audit(alice, path), path)


def test_community_and_conversation_pages_are_usable(client_factory):
    alice = client_factory()
    bob = client_factory()
    assert create_account(alice, "alice@example.com", "alice").status_code == 303
    assert create_account(bob, "bob@example.com", "bob").status_code == 303

    page = alice.get("/communities/new")
    created = alice.post(
        "/communities",
        data={
            "csrf_token": csrf_from(page.text),
            "name": "Accessible Lab",
            "slug": "accessible-lab",
            "description": "Layout checks",
            "visibility": "public",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    detail = alice.get("/communities/accessible-lab")
    posted = alice.post(
        "/communities/accessible-lab/posts",
        data={"csrf_token": csrf_from(detail.text), "title": "First post", "body": "Body text"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    post_path = posted.headers["location"]

    people = alice.get("/people")
    target = people.text.split('@bob', 1)[1].split('action="/conversations/direct/', 1)[1]
    conversation_id = target.split('"', 1)[0]
    started = alice.post(
        f"/conversations/direct/{conversation_id}",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    )
    assert started.status_code == 303

    for path in ("/communities/accessible-lab", post_path, started.headers["location"]):
        assert_page_is_usable(audit(alice, path), path)
