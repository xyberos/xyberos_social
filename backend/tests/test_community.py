import re

from conftest import create_account, csrf_from


def create_community(client, slug: str, visibility: str = "public"):
    page = client.get("/communities/new")
    response = client.post(
        "/communities",
        data={
            "csrf_token": csrf_from(page.text),
            "name": slug.replace("-", " ").title(),
            "slug": slug,
            "description": "A community for thoughtful conversation.",
            "visibility": visibility,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def user_id_from_people_page(page: str, username: str) -> str:
    match = re.search(rf"@{re.escape(username)}.*?action=\"/people/([0-9a-f-]+)/", page, re.S)
    assert match, f"Could not find user id for {username}"
    return match.group(1)


def user_id_from_member_row(page: str, slug: str, username: str) -> str:
    match = re.search(
        rf"@{re.escape(username)}.*?action=\"/communities/{re.escape(slug)}/members/([0-9a-f-]+)/role\"",
        page,
        re.S,
    )
    assert match, f"Could not find role form for {username}"
    return match.group(1)


def test_public_community_can_be_discovered_and_joined(client_factory):
    owner = client_factory()
    member = client_factory()
    assert create_account(owner, "owner@example.com", "owner").status_code == 303
    assert create_account(member, "member@example.com", "member").status_code == 303
    create_community(owner, "public-garden")

    directory = member.get("/communities")
    assert "Public Garden" in directory.text
    detail = member.get("/communities/public-garden")
    assert detail.status_code == 200
    join = member.post(
        "/communities/public-garden/join",
        data={"csrf_token": csrf_from(detail.text)},
        follow_redirects=False,
    )
    assert join.status_code == 303
    assert "your role: member" in member.get("/communities/public-garden").text


def test_private_community_invites_and_owner_controls_roles(client_factory):
    owner = client_factory()
    moderator = client_factory()
    invited = client_factory()
    outsider = client_factory()
    for client, username in [
        (owner, "owner"),
        (moderator, "mod"),
        (invited, "invited"),
        (outsider, "outsider"),
    ]:
        assert create_account(client, f"{username}@example.com", username).status_code == 303
    create_community(owner, "private-workshop", visibility="private")

    assert "private-workshop" not in outsider.get("/communities").text
    assert outsider.get("/communities/private-workshop").status_code == 404
    outsider.get("/communities")
    assert outsider.post(
        "/communities/private-workshop/join",
        data={"csrf_token": csrf_from(outsider.get("/profile").text)},
    ).status_code == 404

    owner_detail = owner.get("/communities/private-workshop")
    invitation = owner.post(
        "/communities/private-workshop/members",
        data={"csrf_token": csrf_from(owner_detail.text), "username": "mod"},
        follow_redirects=False,
    )
    assert invitation.status_code == 303

    owner_detail = owner.get("/communities/private-workshop")
    moderator_id = user_id_from_member_row(owner_detail.text, "private-workshop", "mod")
    promoted = owner.post(
        f"/communities/private-workshop/members/{moderator_id}/role",
        data={"csrf_token": csrf_from(owner_detail.text), "role": "moderator"},
        follow_redirects=False,
    )
    assert promoted.status_code == 303
    assert "role changed" in owner.get("/communities/private-workshop").text.lower()
    assert moderator.get("/communities/private-workshop").status_code == 200
    assert outsider.get("/communities/private-workshop").status_code == 404

    moderator_detail = moderator.get("/communities/private-workshop")
    add_member = moderator.post(
        "/communities/private-workshop/members",
        data={"csrf_token": csrf_from(moderator_detail.text), "username": "invited"},
        follow_redirects=False,
    )
    assert add_member.status_code == 303
    invited_detail = invited.get("/communities/private-workshop")
    assert invited_detail.status_code == 200

    invited_id = user_id_from_member_row(
        owner.get("/communities/private-workshop").text,
        "private-workshop",
        "invited",
    )
    forbidden_role_change = moderator.post(
        f"/communities/private-workshop/members/{invited_id}/role",
        data={"csrf_token": csrf_from(moderator_detail.text), "role": "moderator"},
    )
    assert forbidden_role_change.status_code == 403
    assert "@invited" in owner.get("/communities/private-workshop").text


def test_block_removes_follows_in_both_directions(client_factory):
    blocker = client_factory()
    target = client_factory()
    assert create_account(blocker, "blocker@example.com", "blocker").status_code == 303
    assert create_account(target, "target@example.com", "target").status_code == 303

    blocker_id = user_id_from_people_page(blocker.get("/people").text, "target")
    target_id = user_id_from_people_page(target.get("/people").text, "blocker")
    for client, user_id in [(blocker, blocker_id), (target, target_id)]:
        people = client.get("/people")
        assert client.post(
            f"/people/{user_id}/follow",
            data={"csrf_token": csrf_from(people.text)},
        ).status_code == 200

    people = blocker.get("/people")
    assert blocker.post(
        f"/people/{target_id}/block",
        data={"csrf_token": csrf_from(people.text)},
    ).status_code == 400
    blocker_id = user_id_from_people_page(people.text, "target")
    blocked = blocker.post(
        f"/people/{blocker_id}/block",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    )
    assert blocked.status_code == 303
    assert "Unblock" in blocker.get("/people").text
    target_people = target.get("/people")
    assert "They blocked you" in target_people.text
    re_follow = target.post(
        f"/people/{target_id}/follow",
        data={"csrf_token": csrf_from(target.get("/profile").text)},
        follow_redirects=False,
    )
    assert "A%20block%20prevents%20this%20follow" in re_follow.headers["location"]