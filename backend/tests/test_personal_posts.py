"""Personal updates: a post that belongs to an account instead of a community.

The audience is the author's followers, so the interesting cases are the ones where a
reader is not in that audience: every one of them must fail closed with the same 404 a
missing post returns.
"""

import re

from conftest import create_account, csrf_from


def publish(client, body: str = "Something worth sharing.", title: str = "") -> str:
    page = client.get("/profile")
    response = client.post(
        "/posts",
        data={
            "csrf_token": csrf_from(page.text),
            "title": title,
            "body": body,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"]


def follow(client, username: str) -> str:
    """Follow a person through the directory, returning their user id."""
    people = client.get("/people")
    match = re.search(
        rf"@{re.escape(username)}.*?action=\"/people/([0-9a-f-]+)/follow\"", people.text, re.S
    )
    assert match, f"Could not find {username} in the directory"
    person_id = match.group(1)
    response = client.post(
        f"/people/{person_id}/follow",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return person_id


def register(client, username: str) -> None:
    assert create_account(client, f"{username}@example.com", username).status_code == 303


def test_profile_publishes_and_lists_personal_updates(client_factory):
    author = client_factory()
    register(author, "author")

    profile = author.get("/profile")
    assert 'action="/posts"' in profile.text
    assert "Nothing published yet." in profile.text

    location = publish(author, "A first update.", title="Hello everyone")
    assert location.startswith("/posts/")

    listed = author.get("/profile")
    assert "Hello everyone" in listed.text
    assert "A first update." in listed.text

    detail = author.get(location)
    assert detail.status_code == 200
    assert "Hello everyone" in detail.text
    assert "A first update." in detail.text
    assert "Personal update" in detail.text


def test_personal_update_is_readable_by_followers_only(client_factory):
    author = client_factory()
    follower = client_factory()
    stranger = client_factory()
    anonymous = client_factory()
    register(author, "author")
    register(follower, "follower")
    register(stranger, "stranger")

    location = publish(author, "Followers only.")
    person_id = follow(follower, "author")

    assert follower.get(location).status_code == 200
    # The stranger is signed in and can see the profile, but not the update behind it.
    assert stranger.get(location).status_code == 404
    assert anonymous.get(location).status_code == 404

    stranger_view = stranger.get(f"/people/{person_id}")
    assert stranger_view.status_code == 200
    assert "Followers only." not in stranger_view.text
    assert "Personal updates are shared with followers." in stranger_view.text

    follower_view = follower.get(f"/people/{person_id}")
    assert "Followers only." in follower_view.text


def test_a_block_hides_personal_updates_in_both_directions(client_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")

    location = publish(author, "Not for someone I blocked.")
    follow(reader, "author")
    assert reader.get(location).status_code == 200

    people = reader.get("/people")
    author_id = re.search(r"@author.*?action=\"/people/([0-9a-f-]+)/block\"", people.text, re.S)
    assert author_id
    blocked = reader.post(
        f"/people/{author_id.group(1)}/block",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    )
    assert blocked.status_code == 303

    assert reader.get(location).status_code == 404
    assert "Not for someone I blocked." not in reader.get(
        f"/people/{author_id.group(1)}"
    ).text


def test_followers_feed_includes_personal_updates(client_factory):
    author = client_factory()
    follower = client_factory()
    stranger = client_factory()
    register(author, "author")
    register(follower, "follower")
    register(stranger, "stranger")

    publish(author, "An update from a followed person.")
    follow(follower, "author")

    people_feed = follower.get("/updates?source=people&days=7")
    assert "An update from a followed person." in people_feed.text
    assert "Personal update" in people_feed.text

    # A personal update has no community, so the community source cannot contain it.
    community_feed = follower.get("/updates?source=communities&days=7")
    assert "An update from a followed person." not in community_feed.text

    stranger_feed = stranger.get("/updates?source=all&days=7")
    assert "An update from a followed person." not in stranger_feed.text


def test_author_removes_a_personal_update(client_factory):
    author = client_factory()
    follower = client_factory()
    register(author, "author")
    register(follower, "follower")
    location = publish(author, "This will be withdrawn.")
    follow(follower, "author")

    detail = author.get(location)
    removed = author.post(
        f"{location}/remove",
        data={"csrf_token": csrf_from(detail.text)},
        follow_redirects=False,
    )
    assert removed.status_code == 303
    assert removed.headers["location"] == "/profile"

    assert "This will be withdrawn." not in author.get("/profile").text
    # The author keeps a tombstone; the audience loses the row entirely.
    assert "This update was removed." in author.get(location).text
    assert follower.get(location).status_code == 404


def test_comments_on_a_personal_update(client_factory):
    author = client_factory()
    follower = client_factory()
    stranger = client_factory()
    register(author, "author")
    register(follower, "follower")
    register(stranger, "stranger")
    location = publish(author, "Ask me anything.")
    follow(follower, "author")

    detail = follower.get(location)
    commented = follower.post(
        f"{location}/comments",
        data={"csrf_token": csrf_from(detail.text), "body": "A follow-up question."},
        follow_redirects=False,
    )
    assert commented.status_code == 303
    assert author.get(location).text.count("A follow-up question.") == 1

    # The audience is the only place a comment may be written from.
    stranger_detail = stranger.get(location)
    assert stranger_detail.status_code == 404
    assert (
        stranger.post(
            f"{location}/comments",
            data={"csrf_token": csrf_from(stranger.get("/profile").text), "body": "Uninvited."},
            follow_redirects=False,
        ).status_code
        == 404
    )

    # The author owns the page, so they can clear a reply they did not write.
    comment_view = follower.get(location)
    comment_id = re.search(r'id="comment-([0-9a-f-]+)"', comment_view.text).group(1)
    removed = author.post(
        f"{location}/comments/{comment_id}/remove",
        data={"csrf_token": csrf_from(author.get(location).text)},
        follow_redirects=False,
    )
    assert removed.status_code == 303
    assert "This comment was removed." in author.get(location).text


def test_account_export_includes_personal_updates(client_factory):
    author = client_factory()
    register(author, "author")
    location = publish(author, "Exported as well.", title="Archive me")
    detail = author.get(location)
    author.post(
        f"{location}/comments",
        data={"csrf_token": csrf_from(detail.text), "body": "A reply on my own update."},
        follow_redirects=False,
    )

    payload = author.get("/account/export").json()
    exported = next(post for post in payload["posts"] if post["title"] == "Archive me")
    # The export is the author's own data, so a post with no community still belongs in it.
    assert exported["community"] is None
    assert exported["body"] == "Exported as well."
    assert any(
        comment["body"] == "A reply on my own update." and comment["community"] is None
        for comment in payload["comments"]
    )


def test_personal_update_validation_and_ownership(client_factory):
    author = client_factory()
    other = client_factory()
    register(author, "author")
    register(other, "other")

    profile = author.get("/profile")
    token = csrf_from(profile.text)

    empty = author.post(
        "/posts",
        data={"csrf_token": token, "title": "A title without text", "body": ""},
        follow_redirects=False,
    )
    assert empty.status_code == 303
    assert empty.headers["location"] == "/profile?error=invalid-post"

    too_long = author.post(
        "/posts",
        data={"csrf_token": token, "title": "", "body": "x" * 10001},
        follow_redirects=False,
    )
    assert too_long.headers["location"] == "/profile?error=invalid-post"

    location = publish(author, "Published once.")
    assert author.get("/profile?error=invalid-post").text.count("Write something") == 1

    duplicate = author.post(
        "/posts",
        data={"csrf_token": token, "title": "", "body": "Published once."},
        follow_redirects=False,
    )
    assert duplicate.headers["location"] == "/profile?error=duplicate-post"

    # Only the author may withdraw their own update, and only the author may write it.
    assert other.post(
        f"{location}/remove",
        data={"csrf_token": csrf_from(other.get("/profile").text)},
        follow_redirects=False,
    ).status_code == 404
