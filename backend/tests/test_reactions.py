"""Likes and dislikes, on both kinds of post.

The rules these tests hold: a verdict needs the read rule to admit the reader (and a membership
to take part in a community), the same verdict twice withdraws it, the other verdict replaces it,
a withdrawn post takes no verdict, and a signed-out reader sees the counts without the controls.
Counts are counted from the rows, never stored, so they cannot drift away from them.
"""

from sqlalchemy import select

from app.models import PostReaction
from conftest import create_account, csrf_from
from test_platform_moderation import post_id_of, publish, register


def react(client, location: str, kind: str, *, community: str | None = None):
    post_id = post_id_of(location)
    action = (
        f"/communities/{community}/posts/{post_id}/reactions"
        if community
        else f"/posts/{post_id}/reactions"
    )
    return client.post(
        action,
        data={"csrf_token": csrf_from(client.get("/posts/new").text), "kind": kind},
        follow_redirects=False,
    )


def stored_kinds(database_factory) -> list[str]:
    with database_factory() as database:
        return [row.kind for row in database.scalars(select(PostReaction)).all()]


def a_community(owner, slug: str = "garden") -> str:
    page = owner.get("/communities/new")
    created = owner.post(
        "/communities",
        data={
            "csrf_token": csrf_from(page.text),
            "name": slug.title(),
            "slug": slug,
            "description": "A community for reactions.",
            "visibility": "public",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    page = owner.get(f"/communities/{slug}")
    published = owner.post(
        f"/communities/{slug}/posts",
        data={"csrf_token": csrf_from(page.text), "title": "A topic", "body": "Worth a verdict."},
        follow_redirects=False,
    )
    assert published.status_code == 303
    return published.headers["location"]


def test_a_reader_likes_a_personal_update(client_factory, database_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    location = publish(author, "Worth a like.")

    assert react(reader, location, "like").status_code == 303

    assert stored_kinds(database_factory) == ["like"]
    page = reader.get(location)
    assert 'class="reaction-count">1<' in page.text
    assert 'aria-pressed="true"' in page.text


def test_the_same_verdict_twice_withdraws_it(client_factory, database_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    location = publish(author)

    assert react(reader, location, "like").status_code == 303
    assert react(reader, location, "like").status_code == 303

    assert stored_kinds(database_factory) == []
    assert 'class="reaction-count">0<' in reader.get(location).text


def test_the_other_verdict_replaces_it(client_factory, database_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    location = publish(author)

    assert react(reader, location, "like").status_code == 303
    assert react(reader, location, "dislike").status_code == 303

    # One reader, one verdict: the row changed rather than multiplying.
    assert stored_kinds(database_factory) == ["dislike"]


def test_a_verdict_needs_the_read_rule_and_a_session(client_factory, database_factory):
    author = client_factory()
    stranger = client_factory()
    register(author, "author")
    register(stranger, "stranger")
    followers_only = publish(author, "For followers.", audience="followers")
    public = publish(author, "For anyone.", audience="public")
    anonymous = client_factory()

    # A post the audience keeps out cannot be reacted to, and neither can one without a session.
    assert react(stranger, followers_only, "like").status_code == 404
    assert (
        anonymous.post(
            f"/posts/{post_id_of(public)}/reactions",
            data={"csrf_token": "no-session", "kind": "like"},
            follow_redirects=False,
        ).status_code
        == 403
    )
    assert stored_kinds(database_factory) == []


def test_a_withdrawn_post_takes_no_verdict(client_factory, database_factory):
    author = client_factory()
    register(author, "author")
    location = publish(author)
    author.post(
        f"/posts/{post_id_of(location)}/remove",
        data={"csrf_token": csrf_from(author.get("/posts/new").text)},
        follow_redirects=False,
    )

    assert react(author, location, "like").status_code == 404
    assert stored_kinds(database_factory) == []


def test_a_community_post_needs_a_membership(client_factory, database_factory):
    owner = client_factory()
    reader = client_factory()
    assert create_account(owner, "owner@example.com", "owner").status_code == 303
    register(reader, "reader")
    location = a_community(owner, "garden")

    # Reading a public community is open; taking part in it is not.
    assert reader.get(location).status_code == 200
    assert react(reader, location, "like", community="garden").status_code == 403

    page = reader.get("/communities/garden")
    assert (
        reader.post(
            "/communities/garden/join",
            data={"csrf_token": csrf_from(page.text)},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert react(reader, location, "like", community="garden").status_code == 303
    assert stored_kinds(database_factory) == ["like"]
    # The community page counts the same row the post page does.
    assert 'class="reaction-count">1<' in reader.get("/communities/garden").text


def test_a_signed_out_reader_sees_counts_without_controls(client_factory, database_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    location = publish(author)
    assert react(reader, location, "like").status_code == 303

    page = client_factory().get(location)

    assert page.status_code == 200
    assert 'class="reaction-count">1<' in page.text
    # Nothing to click: a crawler and a signed-out visitor see the same numbers.
    assert "reaction-button" not in page.text


def test_the_feed_and_the_workspace_count_the_same_rows(client_factory, database_factory):
    author = client_factory()
    register(author, "author")
    location = publish(author, "Counted everywhere.")
    assert react(author, location, "like").status_code == 303

    assert 'class="reaction-count">1<' in author.get("/dashboard").text
    assert 'class="reaction-count">1<' in author.get("/workspace").text
