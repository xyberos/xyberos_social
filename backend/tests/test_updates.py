import html
import re

from conftest import create_account, csrf_from


def create_community(client, slug: str, visibility: str = "public") -> None:
    page = client.get("/communities/new")
    response = client.post(
        "/communities",
        data={
            "csrf_token": csrf_from(page.text),
            "name": slug.replace("-", " ").title(),
            "slug": slug,
            "description": "A place for a focused discussion.",
            "visibility": visibility,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def create_post(client, slug: str, title: str) -> None:
    page = client.get(f"/communities/{slug}")
    response = client.post(
        f"/communities/{slug}/posts",
        data={"csrf_token": csrf_from(page.text), "title": title, "body": "Useful context."},
        follow_redirects=False,
    )
    assert response.status_code == 303


def user_id_from_people_page(page: str, username: str) -> str:
    match = re.search(
        rf"@{re.escape(username)}.*?action=\"/people/([0-9a-f-]+)/follow\"", page, re.S
    )
    assert match, f"Could not find user id for {username}"
    return match.group(1)


def test_updates_respect_sources_private_communities_and_blocks(client_factory):
    viewer = client_factory()
    followed = client_factory()
    community_author = client_factory()
    for client, username in [
        (viewer, "viewer"),
        (followed, "followed"),
        (community_author, "communityauthor"),
    ]:
        assert create_account(client, f"{username}@example.com", username).status_code == 303

    create_community(followed, "public-updates")
    create_community(followed, "private-updates", visibility="private")
    create_community(community_author, "joined-updates")
    create_post(followed, "public-updates", "Followed public post")
    create_post(followed, "private-updates", "Followed private post")

    people = viewer.get("/people")
    followed_id = user_id_from_people_page(people.text, "followed")
    assert viewer.post(
        f"/people/{followed_id}/follow",
        data={"csrf_token": csrf_from(people.text)},
    ).status_code == 200
    public_page = viewer.get("/communities/public-updates")
    assert viewer.post(
        "/communities/public-updates/join",
        data={"csrf_token": csrf_from(public_page.text)},
        follow_redirects=False,
    ).status_code == 303
    joined_page = viewer.get("/communities/joined-updates")
    assert viewer.post(
        "/communities/joined-updates/join",
        data={"csrf_token": csrf_from(joined_page.text)},
        follow_redirects=False,
    ).status_code == 303
    create_post(community_author, "joined-updates", "Joined community post")

    people_updates = viewer.get("/updates?source=people&days=7&limit=10")
    assert "Followed public post" in people_updates.text
    assert "Followed private post" not in people_updates.text
    assert "Joined community post" not in people_updates.text

    community_updates = viewer.get("/updates?source=communities&days=7&limit=10")
    assert "Joined community post" in community_updates.text
    assert "Followed public post" in community_updates.text
    assert "Followed private post" not in community_updates.text

    people = viewer.get("/people")
    blocked = viewer.post(
        f"/people/{followed_id}/block",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    )
    assert blocked.status_code == 303
    blocked_updates = viewer.get("/updates?source=communities&days=7&limit=10")
    assert "Followed public post" not in blocked_updates.text
    assert "Joined community post" in blocked_updates.text


def test_updates_are_bounded_and_continue_with_stable_keyset(client_factory):
    client = client_factory()
    assert create_account(client, "updates@example.com", "updatesuser").status_code == 303
    create_community(client, "updates-room")
    for title in ["Window post one", "Window post two", "Window post three"]:
        create_post(client, "updates-room", title)

    settings = client.get("/settings")
    saved = client.post(
        "/settings",
        data={
            "csrf_token": csrf_from(settings.text),
            "updates_source": "communities",
            "updates_days": "7",
            "updates_limit": "1",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303

    first_batch = client.get("/updates")
    assert first_batch.status_code == 200
    assert len(re.findall(r"<article class=\"post-preview\">", first_batch.text)) == 1
    assert "Load next batch" in first_batch.text
    next_url = re.search(r'href="([^"]*cursor=[^"]+)"', first_batch.text).group(1)
    second_batch = client.get(html.unescape(next_url))
    assert second_batch.status_code == 200
    assert len(re.findall(r"<article class=\"post-preview\">", second_batch.text)) == 1
    first_title = re.search(r"<h3><a[^>]*>(.*?)</a></h3>", first_batch.text).group(1)
    second_title = re.search(r"<h3><a[^>]*>(.*?)</a></h3>", second_batch.text).group(1)
    first_id = re.search(r"/posts/([0-9a-f-]+)", first_batch.text).group(1)
    second_id = re.search(r"/posts/([0-9a-f-]+)", second_batch.text).group(1)
    assert first_id != second_id, (first_title, second_title, first_id, second_id)

    assert client.get("/updates?limit=100").status_code == 422
    assert client.get("/updates?cursor=invalid").status_code == 422