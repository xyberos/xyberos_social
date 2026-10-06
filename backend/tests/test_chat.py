import re

import pytest
from starlette.websockets import WebSocketDisconnect

from conftest import create_account, csrf_from


def start_direct(client, target_username: str) -> str:
    people = client.get("/people")
    match = re.search(
        rf"@{re.escape(target_username)}.*?action=\"/conversations/direct/([0-9a-f-]+)\"",
        people.text,
        re.S,
    )
    assert match, f"No direct-message action for @{target_username}"
    response = client.post(
        f"/conversations/direct/{match.group(1)}",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def test_direct_messages_persist_broadcast_and_respect_blocks(client_factory):
    alice = client_factory()
    bob = client_factory()
    outsider = client_factory()
    for client, username in [(alice, "alice"), (bob, "bob"), (outsider, "outsider")]:
        assert create_account(client, f"{username}@example.com", username).status_code == 303

    conversation_id = start_direct(alice, "bob")
    assert start_direct(bob, "alice") == conversation_id
    inbox = alice.get("/conversations")
    assert "Bob" in inbox.text
    detail = alice.get(f"/conversations/{conversation_id}")
    assert detail.status_code == 200
    assert 'data-kind="direct"' in detail.text

    with bob.websocket_connect(
        f"/ws/conversations/{conversation_id}", headers={"origin": "http://testserver"}
    ) as socket:
        response = alice.post(
            f"/conversations/{conversation_id}/messages",
            data={
                "csrf_token": csrf_from(detail.text),
                "body": "Hello from the inbox.",
            },
            headers={"X-Chat-Request": "true"},
        )
        assert response.status_code == 200
        assert response.json()["body"] == "Hello from the inbox."
        packet = socket.receive_json()
        assert packet["type"] == "message"
        assert packet["message"]["body"] == "Hello from the inbox."

    bob_conversation = bob.get(f"/conversations/{conversation_id}")
    assert "Hello from the inbox." in bob_conversation.text
    assert "unread-count" not in bob.get("/conversations").text

    with pytest.raises(WebSocketDisconnect):
        with outsider.websocket_connect(
            f"/ws/conversations/{conversation_id}", headers={"origin": "http://testserver"}
        ) as socket:
            socket.receive_text()

    alice_people = alice.get("/people")
    bob_id = re.search(r'@bob.*?action="/people/([0-9a-f-]+)/', alice_people.text, re.S).group(1)
    assert alice.post(
        f"/people/{bob_id}/block",
        data={"csrf_token": csrf_from(alice_people.text)},
        follow_redirects=False,
    ).status_code == 303
    blocked_send = bob.post(
        f"/conversations/{conversation_id}/messages",
        data={"csrf_token": csrf_from(bob.get("/profile").text), "body": "This should not deliver."},
    )
    assert blocked_send.status_code == 403
    assert "Earlier messages remain available" in bob.get(f"/conversations/{conversation_id}").text


def test_group_conversation_invite_history_and_leave(client_factory):
    owner = client_factory()
    member = client_factory()
    outsider = client_factory()
    for client, username in [(owner, "owner"), (member, "member"), (outsider, "outside")]:
        assert create_account(client, f"{username}@example.com", username).status_code == 303

    new_page = owner.get("/conversations/new")
    create = owner.post(
        "/conversations/groups",
        data={
            "csrf_token": csrf_from(new_page.text),
            "title": "Weekend plans",
            "usernames": "member",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303
    conversation_id = create.headers["location"].rsplit("/", 1)[1]
    owner_page = owner.get(f"/conversations/{conversation_id}")
    assert "Weekend plans" in owner_page.text
    assert member.get(f"/conversations/{conversation_id}").status_code == 200
    assert outsider.get(f"/conversations/{conversation_id}").status_code == 404

    invite = owner.post(
        f"/conversations/{conversation_id}/members",
        data={"csrf_token": csrf_from(owner_page.text), "username": "outside"},
        follow_redirects=False,
    )
    assert invite.status_code == 303
    owner_page = owner.get(f"/conversations/{conversation_id}")
    assert "outside was added to the group" in owner_page.text

    with member.websocket_connect(
        f"/ws/conversations/{conversation_id}", headers={"origin": "http://testserver"}
    ) as socket:
        response = owner.post(
            f"/conversations/{conversation_id}/messages",
            data={"csrf_token": csrf_from(owner_page.text), "body": "Plan is set."},
            headers={"X-Chat-Request": "true"},
        )
        assert response.status_code == 200
        assert socket.receive_json()["message"]["body"] == "Plan is set."

    owner_page = owner.get(f"/conversations/{conversation_id}")
    outside_id = re.search(
        r'@outside.*?action="/conversations/[0-9a-f-]+/members/([0-9a-f-]+)/remove',
        owner_page.text,
        re.S,
    ).group(1)
    with outsider.websocket_connect(
        f"/ws/conversations/{conversation_id}", headers={"origin": "http://testserver"}
    ) as socket:
        remove = owner.post(
            f"/conversations/{conversation_id}/members/{outside_id}/remove",
            data={"csrf_token": csrf_from(owner_page.text)},
            follow_redirects=False,
        )
        assert remove.status_code == 303
        assert socket.receive_json()["type"] == "membership"
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()
    assert outsider.get(f"/conversations/{conversation_id}").status_code == 404
    assert "outside was removed from the group" in owner.get(f"/conversations/{conversation_id}").text
    assert "Plan is set." in owner.get(f"/conversations/{conversation_id}").text

    member_page = member.get(f"/conversations/{conversation_id}")
    leave = member.post(
        f"/conversations/{conversation_id}/leave",
        data={"csrf_token": csrf_from(member_page.text)},
        follow_redirects=False,
    )
    assert leave.status_code == 303
    assert member.get(f"/conversations/{conversation_id}").status_code == 404
    assert owner.get(f"/conversations/{conversation_id}").status_code == 200
