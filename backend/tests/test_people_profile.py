"""A person's profile, reached from the directory, shows exactly what its audience allows.

The landing page is `/people/{id}`. These tests hold the two halves of that promise: the reader
is offered the action from the directory, and the page then shows only the updates their
relationship reaches — while saying that restricted updates exist, without saying how many.
"""

from test_personal_posts import (
    follow,
    person_id,
    publish_with_audience,
    register,
)


def test_the_directory_offers_a_view_profile_action(client_factory):
    owner = client_factory()
    reader = client_factory()
    register(owner, "owner")
    register(reader, "reader")
    author_id = person_id(reader, "owner")

    directory = reader.get("/people")

    assert f'href="/people/{author_id}"' in directory.text
    assert "View profile" in directory.text


def test_a_profile_needs_an_account(client_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    author_id = person_id(reader, "author")

    # The directory itself is behind the sign-in wall, and so is every profile it links to.
    response = client_factory().get(f"/people/{author_id}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_profile_shows_only_what_the_viewer_may_read(client_factory):
    author = client_factory()
    follower = client_factory()
    stranger = client_factory()
    register(author, "author")
    register(follower, "follower")
    register(stranger, "stranger")

    publish_with_audience(author, "public", "Open to the web.")
    publish_with_audience(author, "everyone", "Open to every account.")
    publish_with_audience(author, "followers", "For my followers.")
    publish_with_audience(author, "only_me", "Kept to myself.")
    follow(follower, "author")
    author_id = person_id(stranger, "author")

    stranger_view = stranger.get(f"/people/{author_id}").text
    assert "Open to the web." in stranger_view
    assert "Open to every account." in stranger_view
    assert "For my followers." not in stranger_view
    assert "Kept to myself." not in stranger_view

    follower_view = follower.get(f"/people/{author_id}").text
    assert "For my followers." in follower_view
    assert "Kept to myself." not in follower_view

    own_view = author.get(f"/people/{author_id}", follow_redirects=False)
    assert own_view.status_code == 303
    assert own_view.headers["location"] == "/profile"


def test_the_profile_says_when_updates_are_restricted(client_factory):
    author = client_factory()
    stranger = client_factory()
    register(author, "author")
    register(stranger, "stranger")

    publish_with_audience(author, "public", "One you can read.")
    publish_with_audience(author, "followers", "One you cannot.")
    author_id = person_id(stranger, "author")

    page = stranger.get(f"/people/{author_id}").text

    # The visible update is there, and the page still accounts for the one that is not.
    assert "One you can read." in page
    assert "One you cannot." not in page
    assert "Some updates are shared with followers." in page
    assert "Follow Author to read them." in page


def test_a_follower_is_told_what_their_follow_does_not_reach(client_factory):
    author = client_factory()
    follower = client_factory()
    register(author, "author")
    register(follower, "follower")
    publish_with_audience(author, "followers", "For followers.")
    publish_with_audience(author, "only_me", "For nobody else.")
    # Following moves them into the Following group, where the row offers "unfollow" instead,
    # so the id comes back from the action that created it.
    author_id = follow(follower, "author")

    page = follower.get(f"/people/{author_id}").text

    assert "For followers." in page
    assert "For nobody else." not in page
    # Following is not the remedy here, so the page does not offer it as one.
    assert "Some updates here are kept private by their author." in page
    assert "to read them." not in page
