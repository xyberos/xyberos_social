"""Personal updates: a post that belongs to an account instead of a community.

An update has an audience — its author always, and then only me, followers, everyone, or the
open web — so the interesting cases are the readers the choice excludes: every one of them must
fail closed with the same 404 a missing post returns, and the author's page must show each
reader exactly the updates the post page would let them open. `public` is the one value a
signed-out reader can satisfy, which is why it is also the only one a crawler may index.
"""

import html
import re

from sqlalchemy import select

from app.models import Post, User
from conftest import create_account, csrf_from


def workspace_titles(page: str) -> set[str]:
    """The post titles on a workspace page, in document order."""
    return set(re.findall(r'class="workspace-title">\s*<a[^>]*>([^<]+)</a>', page))


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


def publish_with_audience(client, audience: str, body: str = "Addressed.") -> str:
    """Publish a personal update with an explicit audience, returning its URL."""
    page = client.get("/posts/new")
    response = client.post(
        "/posts",
        data={
            "csrf_token": csrf_from(page.text),
            "title": "",
            "body": body,
            "audience": audience,
            "return_to": "/posts/new",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"]


def person_id(client, username: str) -> str:
    """The account id behind a username, read from the directory without following."""
    people = client.get("/people")
    match = re.search(
        rf"@{re.escape(username)}.*?action=\"/people/([0-9a-f-]+)/follow\"", people.text, re.S
    )
    assert match, f"Could not find {username} in the directory"
    return match.group(1)


def follow(client, username: str) -> str:
    """Follow a person through the directory, returning their user id."""
    people = client.get("/people")
    person = person_id(client, username)
    response = client.post(
        f"/people/{person}/follow",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return person


def register(client, username: str) -> None:
    assert create_account(client, f"{username}@example.com", username).status_code == 303


def test_the_workspace_lists_what_the_author_published(client_factory):
    author = client_factory()
    register(author, "author")

    profile = author.get("/profile")
    assert 'href="/workspace"' in profile.text
    # The list of what the author published lives on the workspace, not on the profile.
    assert "Nothing published yet." not in profile.text

    workspace = author.get("/workspace")
    assert workspace.status_code == 200
    assert "Nothing published yet." in workspace.text
    assert 'href="/posts/new"' in workspace.text

    location = publish(author, "A first update.", title="Hello everyone")
    assert location.startswith("/posts/")

    listed = author.get("/workspace")
    assert "Hello everyone" in listed.text
    assert "A first update." in listed.text
    assert "Showing 1 of 1 post" in listed.text

    detail = author.get(location)
    assert detail.status_code == 200
    assert "Hello everyone" in detail.text
    assert "A first update." in detail.text
    assert "Personal update" in detail.text


def test_the_workspace_filters_both_kinds_of_post(client_factory):
    """A personal update and a community post are rows in the same workspace."""
    author = client_factory()
    register(author, "author")

    publish(author, "A personal update.", title="From my desk")
    page = author.get("/communities/new")
    assert author.post(
        "/communities",
        data={
            "csrf_token": csrf_from(page.text),
            "name": "Workspace Room",
            "slug": "workspace-room",
            "description": "One workspace.",
            "visibility": "public",
        },
        follow_redirects=False,
    ).status_code == 303
    community_page = author.get("/communities/workspace-room")
    assert author.post(
        "/communities/workspace-room/posts",
        data={
            "csrf_token": csrf_from(community_page.text),
            "title": "From a community",
            "body": "Published in a community.",
        },
        follow_redirects=False,
    ).status_code == 303

    everything = author.get("/workspace")
    assert "From my desk" in everything.text
    assert "From a community" in everything.text
    assert "Showing 2 of 2 posts" in everything.text

    personal = author.get("/workspace?source=personal")
    assert "From my desk" in personal.text
    assert "From a community" not in personal.text
    assert "Showing 1 of 1 post" in personal.text

    community = author.get("/workspace?source=communities")
    assert "From a community" in community.text
    assert "From my desk" not in community.text
    # A community post's audience is its community, so only a personal row offers the control.
    assert 'class="workspace-audience"' in personal.text
    assert 'class="workspace-audience"' not in community.text

    assert author.get("/workspace?source=nonsense").status_code == 422
    assert author.get("/workspace?cursor=invalid").status_code == 422


def test_the_workspace_keeps_a_post_from_a_community_the_author_left(client_factory):
    """Leaving a private community keeps the row, but it stops being a link or an action."""
    owner = client_factory()
    member = client_factory()
    register(owner, "owner")
    register(member, "member")

    page = owner.get("/communities/new")
    assert owner.post(
        "/communities",
        data={
            "csrf_token": csrf_from(page.text),
            "name": "Private Room",
            "slug": "private-room",
            "description": "Members only.",
            "visibility": "private",
        },
        follow_redirects=False,
    ).status_code == 303
    invited = owner.get("/communities/private-room")
    assert owner.post(
        "/communities/private-room/members",
        data={"csrf_token": csrf_from(invited.text), "username": "member"},
        follow_redirects=False,
    ).status_code == 303

    member_page = member.get("/communities/private-room")
    assert "Members only." in member_page.text
    assert member.post(
        "/communities/private-room/posts",
        data={
            "csrf_token": csrf_from(member_page.text),
            "title": "Inside a private room",
            "body": "Published behind a door.",
        },
        follow_redirects=False,
    ).status_code == 303

    while_member = member.get("/workspace")
    assert "Inside a private room" in while_member.text
    assert ">Withdraw</button>" in while_member.text

    assert member.post(
        "/communities/private-room/leave",
        data={"csrf_token": csrf_from(member_page.text)},
        follow_redirects=False,
    ).status_code == 303

    after_leaving = member.get("/workspace")
    assert "Inside a private room" in after_leaving.text
    assert "Community left" in after_leaving.text
    assert ">Withdraw</button>" not in after_leaving.text


def test_the_workspace_pages_through_a_full_window(client_factory, database_factory):
    """The next page continues from a stable keyset rather than from an offset."""
    author = client_factory()
    register(author, "author")

    # The publish endpoint is rate limited, so fill the window directly.
    with database_factory() as database:
        user = database.scalar(select(User).where(User.username == "author"))
        for index in range(51):
            database.add(
                Post(
                    community_id=None,
                    author_id=user.id,
                    title=f"Post {index:02d}",
                    body="Published in bulk.",
                )
            )
        database.commit()

    first = author.get("/workspace")
    assert first.status_code == 200
    assert "Showing 50 of 51 posts" in first.text
    assert "Load more" in first.text
    first_titles = workspace_titles(first.text)
    assert len(first_titles) == 50

    next_url = html.unescape(
        re.search(r'href="(/workspace\?[^"]*cursor=[^"]+)"', first.text).group(1)
    )
    second = author.get(next_url)
    assert second.status_code == 200
    assert "Showing 1 of 51 posts" in second.text
    assert "Load more" not in second.text
    second_titles = workspace_titles(second.text)
    assert len(second_titles) == 1

    # The window is a partition: no row repeats, and the same cursor replays the same page.
    assert not (first_titles & second_titles)
    assert workspace_titles(author.get(next_url).text) == second_titles


def test_the_workspace_needs_an_account(client_factory):
    anonymous = client_factory()

    response = anonymous.get("/workspace", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_composer_offers_four_audiences(client_factory):
    author = client_factory()
    register(author, "author")

    compose = author.get("/posts/new")
    assert 'name="audience"' in compose.text
    assert 'value="followers" selected' in compose.text
    assert 'value="only_me"' in compose.text
    assert 'value="everyone"' in compose.text
    assert 'value="public"' in compose.text

    refused = author.post(
        "/posts",
        data={
            "csrf_token": csrf_from(compose.text),
            "title": "",
            "body": "Who is this for?",
            "audience": "friends",
            "return_to": "/posts/new",
        },
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert refused.headers["location"] == "/posts/new?error=invalid-audience"
    assert "Choose who can see this update." in author.get(
        "/posts/new?error=invalid-audience"
    ).text


def test_each_audience_opens_exactly_the_readers_it_names(client_factory):
    author = client_factory()
    follower = client_factory()
    stranger = client_factory()
    register(author, "author")
    register(follower, "follower")
    register(stranger, "stranger")

    shared = publish_with_audience(author, "everyone", "Anyone may read this.")
    followers_only = publish_with_audience(author, "followers", "Followers may read this.")
    kept = publish_with_audience(author, "only_me", "Nobody else reads this.")
    author_id = person_id(stranger, "author")
    follow(follower, "author")

    # The post page and the author's page answer the same question the same way, so a reader
    # never learns about an update from a list that the page would refuse them.
    for reader, readable in [
        (follower, (shared, followers_only)),
        (stranger, (shared,)),
    ]:
        for location in (shared, followers_only, kept):
            assert reader.get(location).status_code == (200 if location in readable else 404)

        page = reader.get(f"/people/{author_id}")
        assert page.status_code == 200
        for location, marker in [
            (shared, "Anyone may read this."),
            (followers_only, "Followers may read this."),
            (kept, "Nobody else reads this."),
        ]:
            assert (marker in page.text) is (location in readable), marker

    for location in (shared, followers_only, kept):
        assert author.get(location).status_code == 200
    workspace = author.get("/workspace")
    for marker in (
        "Anyone may read this.",
        "Followers may read this.",
        "Nobody else reads this.",
    ):
        assert marker in workspace.text


def test_only_me_stays_out_of_a_followers_window(client_factory):
    author = client_factory()
    follower = client_factory()
    register(author, "author")
    register(follower, "follower")
    follow(follower, "author")

    publish_with_audience(author, "only_me", "Kept to myself.")
    publish_with_audience(author, "followers", "For my followers.")

    window = follower.get("/dashboard")
    assert "For my followers." in window.text
    assert "Kept to myself." not in window.text

    # The author reads their own update back whatever the audience says.
    assert "Kept to myself." in author.get("/dashboard").text


def test_the_author_changes_the_audience_from_the_workspace(client_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")

    location = publish_with_audience(author, "followers", "Decided later.")
    assert reader.get(location).status_code == 404

    workspace = author.get("/workspace")
    assert f'action="{location}/audience"' in workspace.text

    changed = author.post(
        f"{location}/audience",
        data={
            "csrf_token": csrf_from(workspace.text),
            "audience": "everyone",
            "return_to": "/workspace",
        },
        follow_redirects=False,
    )
    assert changed.status_code == 303
    assert changed.headers["location"] == "/workspace"
    assert reader.get(location).status_code == 200

    # A reader may read it, but only the author decides who reads it, and only a known
    # audience is a choice.
    assert reader.post(
        f"{location}/audience",
        data={"csrf_token": csrf_from(reader.get("/profile").text), "audience": "only_me"},
        follow_redirects=False,
    ).status_code == 403
    assert author.post(
        f"{location}/audience",
        data={"csrf_token": csrf_from(author.get("/workspace").text), "audience": "friends"},
        follow_redirects=False,
    ).status_code == 422


def test_a_block_hides_an_everyone_post(client_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")

    location = publish_with_audience(author, "everyone", "Anyone may read this.")
    assert reader.get(location).status_code == 200

    # A block is absolute, so it hides even a post whose audience is everyone.
    people = reader.get("/people")
    assert reader.post(
        f"/people/{person_id(reader, 'author')}/block",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    ).status_code == 303
    assert reader.get(location).status_code == 404


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

    people_feed = follower.get("/dashboard?source=people&days=7")
    assert "An update from a followed person." in people_feed.text
    # A personal update carries no community label, so its own page is what the feed links to.
    assert 'class="post-excerpt"' in people_feed.text

    # A personal update has no community, so the community source cannot contain it.
    community_feed = follower.get("/dashboard?source=communities&days=7")
    assert "An update from a followed person." not in community_feed.text

    stranger_feed = stranger.get("/dashboard?source=all&days=7")
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
    assert removed.headers["location"] == "/workspace"

    # The author keeps a tombstone in the workspace, without a way to withdraw it twice;
    # the audience loses the row entirely.
    workspace = author.get("/workspace")
    assert "This will be withdrawn." in workspace.text
    assert "Withdrawn" in workspace.text
    assert ">Withdraw</button>" not in workspace.text
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
    assert empty.headers["location"] == "/posts/new?error=invalid-post"

    too_long = author.post(
        "/posts",
        data={"csrf_token": token, "title": "", "body": "x" * 10001},
        follow_redirects=False,
    )
    assert too_long.headers["location"] == "/posts/new?error=invalid-post"

    location = publish(author, "Published once.")
    assert author.get("/posts/new?error=invalid-post").text.count("Write something") == 1

    duplicate = author.post(
        "/posts",
        data={"csrf_token": token, "title": "", "body": "Published once."},
        follow_redirects=False,
    )
    assert duplicate.headers["location"] == "/posts/new?error=duplicate-post"

    # Only the author may withdraw their own update, and only the author may write it.
    assert other.post(
        f"{location}/remove",
        data={"csrf_token": csrf_from(other.get("/profile").text)},
        follow_redirects=False,
    ).status_code == 404


def test_the_compose_view_publishes_into_your_own_window(client_factory):
    """The compose view writes the update the dashboard then reads back."""
    author = client_factory()
    register(author, "author")

    dashboard = author.get("/dashboard")
    assert 'href="/posts/new"' in dashboard.text

    compose = author.get("/posts/new")
    assert compose.status_code == 200
    assert 'action="/posts"' in compose.text

    published = author.post(
        "/posts",
        data={
            "csrf_token": csrf_from(compose.text),
            "title": "From the compose view",
            "body": "Published where I read it.",
            "return_to": "/posts/new",
        },
        follow_redirects=False,
    )
    assert published.status_code == 303
    assert published.headers["location"].startswith("/posts/")

    # Publish lands on the new post's own view, which is the same view a community post uses.
    post_view = author.get(published.headers["location"])
    assert post_view.status_code == 200
    assert "From the compose view" in post_view.text
    assert "Published where I read it." in post_view.text

    window = author.get("/dashboard")
    assert "From the compose view" in window.text
    assert "Published where I read it." in window.text


def test_a_failed_publish_returns_to_the_composer_that_submitted_it(client_factory):
    author = client_factory()
    register(author, "author")

    compose = author.get("/posts/new")
    token = csrf_from(compose.text)

    refused = author.post(
        "/posts",
        data={"csrf_token": token, "title": "", "body": "", "return_to": "/posts/new"},
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert refused.headers["location"] == "/posts/new?error=invalid-post"
    assert author.get("/posts/new?error=invalid-post").text.count("Write something") == 1

    # The return path is an allowlist, so a form cannot turn a refusal into an open redirect.
    elsewhere = author.post(
        "/posts",
        data={"csrf_token": token, "title": "", "body": "", "return_to": "https://example.com"},
        follow_redirects=False,
    )
    assert elsewhere.headers["location"] == "/posts/new?error=invalid-post"


def test_the_post_view_is_shared_by_personal_updates_and_community_posts(client_factory):
    """Both kinds of post render the same view, so the reader sees one layout either way."""
    author = client_factory()
    register(author, "author")

    personal = publish(author, "A personal update.")

    page = author.get("/communities/new")
    created = author.post(
        "/communities",
        data={
            "csrf_token": csrf_from(page.text),
            "name": "Shared View",
            "slug": "shared-view",
            "description": "One post view.",
            "visibility": "public",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    community_page = author.get("/communities/shared-view")
    community_post = author.post(
        "/communities/shared-view/posts",
        data={
            "csrf_token": csrf_from(community_page.text),
            "title": "A community post",
            "body": "Same view as a personal update.",
        },
        follow_redirects=False,
    )
    assert community_post.status_code == 303

    for location in (personal, community_post.headers["location"]):
        view = author.get(location)
        assert view.status_code == 200
        assert '<article class="thread-post">' in view.text
        assert '<div class="comment-list">' in view.text
        assert 'class="comment-compose"' in view.text


def test_a_public_update_is_readable_without_an_account(client_factory):
    author = client_factory()
    stranger = client_factory()
    anonymous = client_factory()
    register(author, "author")
    register(stranger, "stranger")

    location = publish_with_audience(author, "public", "Open to the web.")

    page = anonymous.get(location)
    assert page.status_code == 200
    assert "Open to the web." in page.text
    # The open web is exactly this one audience; the others stay behind the account they name.
    for audience in ("followers", "everyone", "only_me"):
        closed = publish_with_audience(author, audience, f"Closed to the web: {audience}.")
        assert anonymous.get(closed).status_code == 404, audience
    assert stranger.get(location).status_code == 200
    # Only the audience the web can read may enter an index.
    assert '<meta name="robots" content="index, follow">' in page.text
    closed_page = stranger.get(publish_with_audience(author, "everyone", "Signed-in only."))
    assert '<meta name="robots" content="noindex, nofollow">' in closed_page.text


def test_a_public_update_can_be_opened_and_closed_again(client_factory):
    author = client_factory()
    anonymous = client_factory()
    register(author, "author")
    location = publish_with_audience(author, "followers", "Decided later.")
    assert anonymous.get(location).status_code == 404

    for audience, reachable in (("public", 200), ("followers", 404)):
        workspace = author.get("/workspace")
        changed = author.post(
            f"{location}/audience",
            data={
                "csrf_token": csrf_from(workspace.text),
                "audience": audience,
                "return_to": "/workspace",
            },
            follow_redirects=False,
        )
        assert changed.status_code == 303
        assert anonymous.get(location).status_code == reachable, audience


def test_a_block_still_hides_a_public_update_from_the_blocked_reader(client_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")

    location = publish_with_audience(author, "public", "Open to the web.")
    assert reader.get(location).status_code == 200

    people = reader.get("/people")
    assert (
        reader.post(
            f"/people/{person_id(reader, 'author')}/block",
            data={"csrf_token": csrf_from(people.text)},
            follow_redirects=False,
        ).status_code
        == 303
    )

    # A block is a statement about a person, so it outranks an audience the web may read.
    assert reader.get(location).status_code == 404


def test_a_public_update_leaves_with_its_account(client_factory, database_factory):
    author = client_factory()
    anonymous = client_factory()
    register(author, "author")
    location = publish_with_audience(author, "public", "Open to the web.")
    assert anonymous.get(location).status_code == 200

    # Deletion keeps the row and erases the account; nothing it published stays readable.
    with database_factory() as database:
        database.scalar(select(User).where(User.username == "author")).is_active = False
        database.commit()

    assert anonymous.get(location).status_code == 404
