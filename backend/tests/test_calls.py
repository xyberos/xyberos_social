"""Phase 9: call records, ring/join/leave, the ceiling, blocks, TURN, and signaling.

The media is a mesh of browser peer connections, so these tests cover everything the server
owns: who may ring whom, what a call looks like while it runs, what happens when it is
declined, missed, blocked, or over the ceiling, and which packets may be relayed. No test
asserts on audio itself, because none of it ever reaches the application.
"""

import asyncio
import base64
import hashlib
import hmac
import re
import time
from uuid import UUID

from sqlalchemy import select

from conftest import create_account, csrf_from

from app.core import metrics
from app.core.config import get_settings
from app.models import Call, CallParticipant, Notification, OutboxEvent, User
from app.services import calls as calls_service

ORIGIN = {"origin": "http://testserver"}
SETTINGS_FORM = {
    "updates_source": "all",
    "updates_days": "7",
    "updates_limit": "20",
}


def sign_up(client_factory, *usernames):
    clients = {}
    for username in usernames:
        client = client_factory()
        assert create_account(client, f"{username}@example.com", username).status_code == 303
        clients[username] = client
    return clients


def start_direct(client, target_username: str) -> str:
    people = client.get("/people")
    target = re.search(
        rf"@{re.escape(target_username)}.*?action=\"/conversations/direct/([0-9a-f-]+)\"",
        people.text,
        re.S,
    ).group(1)
    response = client.post(
        f"/conversations/direct/{target}",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def start_group(owner, title: str, usernames: str) -> str:
    page = owner.get("/conversations/new")
    created = owner.post(
        "/conversations/groups",
        data={"csrf_token": csrf_from(page.text), "title": title, "usernames": usernames},
        follow_redirects=False,
    )
    assert created.status_code == 303
    return created.headers["location"].rsplit("/", 1)[1]


def ring(client, conversation_id: str, media: str = "audio"):
    return client.post(f"/conversations/{conversation_id}/calls", json={"media": media})


def row_for(payload: dict, username: str) -> dict:
    return next(row for row in payload["participants"] if row["username"] == username)


def user_id(database, username: str) -> str:
    return str(database.scalar(select(User.id).where(User.username == username)))


def save_call_settings(client, **toggles):
    page = client.get("/settings")
    return client.post(
        "/settings",
        data={"csrf_token": csrf_from(page.text), **SETTINGS_FORM, **toggles},
        follow_redirects=False,
    )


def test_a_direct_call_rings_records_and_ends(client_factory, database_factory):
    people = sign_up(client_factory, "alice", "bob")
    alice, bob = people["alice"], people["bob"]
    conversation_id = start_direct(alice, "bob")

    page = alice.get(f"/conversations/{conversation_id}")
    assert 'id="start-audio-call"' in page.text
    assert 'id="start-video-call"' in page.text
    # The safety-number module has to be defined before the chat client that calls it.
    assert page.text.index("/static/js/safety-number.js") < page.text.index("/static/js/chat.js")
    # The conversation is the one page allowed to ask for the camera and microphone.
    policy = page.headers["permissions-policy"]
    assert "camera=(self)" in policy
    assert "microphone=(self)" in policy
    assert "microphone=()" in alice.get("/conversations").headers["permissions-policy"]

    with bob.websocket_connect(
        f"/ws/conversations/{conversation_id}", headers=ORIGIN
    ) as callee:
        created = ring(alice, conversation_id, "video")
        assert created.status_code == 201, created.text
        call = created.json()
        assert call["state"] == "ringing"
        assert call["media"] == "video"
        assert call["kind"] == "direct"
        assert row_for(call, "alice")["state"] == "joined"
        assert row_for(call, "bob")["state"] == "invited"

        ringing = callee.receive_json()
        assert ringing["type"] == "call_state"
        assert ringing["call"]["id"] == call["id"]
        assert ringing["call"]["participants"] == call["participants"]

        accepted = bob.post(f"/calls/{call['id']}/accept")
        assert accepted.status_code == 200
        assert accepted.json()["state"] == "accepted"
        assert accepted.json()["answered_at"] is not None
        assert callee.receive_json()["call"]["state"] == "accepted"

        ended = alice.post(f"/calls/{call['id']}/end", json={"reason": "hangup"})
        assert ended.status_code == 200
        assert ended.json()["state"] == "ended"
        assert ended.json()["end_reason"] == "hangup"
        assert callee.receive_json()["call"]["state"] == "ended"

    with database_factory() as database:
        record = database.get(Call, UUID(call["id"]))
        assert record.state == "ended"
        assert record.end_reason == "hangup"
        assert record.answered_at is not None
        assert record.ended_at is not None
        rows = database.scalars(
            select(CallParticipant).where(CallParticipant.call_id == record.id)
        ).all()
        assert {row.state for row in rows} == {"left"}
        assert all(row.joined_at is not None and row.left_at is not None for row in rows)


def test_declining_cancelling_and_missing_are_recorded_and_notified(client_factory):
    people = sign_up(client_factory, "alice", "bob")
    alice, bob = people["alice"], people["bob"]
    conversation_id = start_direct(alice, "bob")

    first = ring(alice, conversation_id).json()
    declined = bob.post(f"/calls/{first['id']}/decline")
    assert declined.status_code == 200
    assert declined.json()["state"] == "ended"
    assert declined.json()["end_reason"] == "declined"
    # Declining is a decision, not a missed call.
    assert bob.get("/api/v1/notifications").json()["count"] == 0

    second = ring(alice, conversation_id).json()
    cancelled = alice.post(f"/calls/{second['id']}/end")
    assert cancelled.json()["end_reason"] == "cancelled"

    notices = bob.get("/api/v1/notifications").json()
    assert notices["count"] == 1
    notice = notices["notifications"][0]
    assert notice["category"] == "call"
    assert notice["message"] == "Missed call from Alice"
    assert notice["target_url"] == f"/conversations/{conversation_id}"

    inbox = bob.get("/notifications")
    assert "Open conversation" in inbox.text
    assert "Missed calls" in bob.get("/settings").text


def test_an_unanswered_ring_expires_as_a_missed_call(client_factory, database_factory, monkeypatch):
    people = sign_up(client_factory, "alice", "bob")
    alice, bob = people["alice"], people["bob"]
    conversation_id = start_direct(alice, "bob")

    settings = get_settings().model_copy(update={"call_ring_timeout_seconds": 0})
    monkeypatch.setattr(calls_service, "get_settings", lambda: settings)

    call = ring(alice, conversation_id).json()
    assert call["state"] == "ringing"

    current = alice.get(f"/conversations/{conversation_id}/calls/current")
    assert current.status_code == 200
    assert current.json() == {"call": None}

    with database_factory() as database:
        record = database.get(Call, UUID(call["id"]))
        assert record.state == "ended"
        assert record.end_reason == "missed"
        rows = database.scalars(
            select(CallParticipant).where(CallParticipant.call_id == record.id)
        ).all()
        assert {row.state for row in rows} == {"left", "missed"}
        assert database.scalar(
            select(Notification.category).where(Notification.category == "call")
        ) == "call"
    assert bob.get("/api/v1/notifications").json()["count"] == 1


def test_missed_call_notifications_can_be_turned_off(client_factory):
    people = sign_up(client_factory, "alice", "bob")
    alice, bob = people["alice"], people["bob"]
    conversation_id = start_direct(alice, "bob")

    assert save_call_settings(bob, accept_calls="true").status_code == 303
    call = ring(alice, conversation_id).json()
    assert alice.post(f"/calls/{call['id']}/end").status_code == 200
    assert bob.get("/api/v1/notifications").json()["count"] == 0


def test_a_reader_who_refuses_calls_is_not_rung(client_factory):
    people = sign_up(client_factory, "alice", "bob")
    alice, bob = people["alice"], people["bob"]
    conversation_id = start_direct(alice, "bob")

    # The checkbox is absent from the form, so accepting calls is turned off.
    assert save_call_settings(bob, notify_calls="true").status_code == 303
    refused = ring(alice, conversation_id)
    assert refused.status_code == 409
    assert "Nobody in this conversation" in refused.json()["detail"]

    assert save_call_settings(bob, accept_calls="true", notify_calls="true").status_code == 303
    assert ring(alice, conversation_id).status_code == 201


def test_blocks_stop_calls_in_both_directions_and_end_an_active_one(
    client_factory, database_factory
):
    people = sign_up(client_factory, "alice", "bob")
    alice, bob = people["alice"], people["bob"]
    conversation_id = start_direct(alice, "bob")
    with database_factory() as database:
        bob_id = user_id(database, "bob")

    active = ring(alice, conversation_id).json()
    assert bob.post(f"/calls/{active['id']}/accept").status_code == 200

    with alice.websocket_connect(
        f"/ws/conversations/{conversation_id}", headers=ORIGIN
    ) as socket:
        people_page = alice.get("/people")
        blocked = alice.post(
            f"/people/{bob_id}/block",
            data={"csrf_token": csrf_from(people_page.text)},
            follow_redirects=False,
        )
        assert blocked.status_code == 303
        ended = socket.receive_json()
        assert ended["type"] == "call_state"
        assert ended["call"]["state"] == "ended"

    with database_factory() as database:
        record = database.get(Call, UUID(active["id"]))
        assert record.state == "ended"
        rows = database.scalars(
            select(CallParticipant).where(CallParticipant.call_id == record.id)
        ).all()
        assert {row.state for row in rows} == {"left"}

    assert ring(alice, conversation_id).status_code == 403
    assert ring(bob, conversation_id).status_code == 403
    assert 'id="start-audio-call"' not in alice.get(f"/conversations/{conversation_id}").text


def test_group_calls_enforce_the_ceiling(client_factory, monkeypatch):
    people = sign_up(client_factory, "owner", "ana", "ben")
    conversation_id = start_group(people["owner"], "Weekend plans", "ana ben")

    settings = get_settings().model_copy(update={"call_max_participants": 2})
    monkeypatch.setattr(calls_service, "get_settings", lambda: settings)
    refused = ring(people["owner"], conversation_id)
    assert refused.status_code == 409
    assert "limited to 2 participants" in refused.json()["detail"]


def test_the_ceiling_is_checked_again_at_join_time(client_factory, monkeypatch):
    people = sign_up(client_factory, "owner", "ana", "ben")
    conversation_id = start_group(people["owner"], "Weekend plans", "ana ben")

    call = ring(people["owner"], conversation_id).json()
    assert len(call["participants"]) == 3

    # The ceiling narrows between ring and join, the way a configuration change would.
    settings = get_settings().model_copy(update={"call_max_participants": 2})
    monkeypatch.setattr(calls_service, "get_settings", lambda: settings)

    assert people["ana"].post(f"/calls/{call['id']}/accept").status_code == 200
    refused = people["ben"].post(f"/calls/{call['id']}/accept")
    assert refused.status_code == 409
    assert refused.json()["detail"] == "This call is full."


def test_a_group_call_rings_every_eligible_participant(client_factory):
    people = sign_up(client_factory, "owner", "ana", "ben")
    owner, ana, ben = people["owner"], people["ana"], people["ben"]
    conversation_id = start_group(owner, "Weekend plans", "ana ben")

    with ana.websocket_connect(
        f"/ws/conversations/{conversation_id}", headers=ORIGIN
    ) as ana_socket, ben.websocket_connect(
        f"/ws/conversations/{conversation_id}", headers=ORIGIN
    ) as ben_socket:
        call = ring(owner, conversation_id).json()
        assert call["kind"] == "group"
        for socket in (ana_socket, ben_socket):
            event = socket.receive_json()
            assert event["call"]["state"] == "ringing"
            assert len(event["call"]["participants"]) == 3

        assert ana.post(f"/calls/{call['id']}/accept").status_code == 200
        for socket in (ana_socket, ben_socket):
            event = socket.receive_json()
            assert event["call"]["state"] == "accepted"
            assert row_for(event["call"], "ana")["state"] == "joined"

        # One participant declining leaves the call running for the others.
        declined = ben.post(f"/calls/{call['id']}/decline")
        assert declined.json()["state"] == "accepted"
        assert row_for(declined.json(), "ben")["state"] == "declined"


def test_a_busy_reader_is_not_invited(client_factory):
    people = sign_up(client_factory, "alice", "bob", "carol")
    alice, bob = people["alice"], people["bob"]
    alice_conversation = start_direct(alice, "bob")
    bob_conversation = start_direct(bob, "carol")

    assert ring(bob, bob_conversation).status_code == 201
    refused = ring(alice, alice_conversation)
    assert refused.status_code == 409
    assert "Nobody in this conversation" in refused.json()["detail"]


def test_signaling_relays_only_between_joined_participants(client_factory, database_factory):
    people = sign_up(client_factory, "owner", "ana", "ben")
    owner, ana = people["owner"], people["ana"]
    conversation_id = start_group(owner, "Trio", "ana ben")
    with database_factory() as database:
        owner_id = user_id(database, "owner")
        ana_id = user_id(database, "ana")
        ben_id = user_id(database, "ben")

    call = ring(owner, conversation_id).json()
    assert ana.post(f"/calls/{call['id']}/accept").status_code == 200

    with owner.websocket_connect(
        f"/ws/conversations/{conversation_id}", headers=ORIGIN
    ) as owner_socket, ana.websocket_connect(
        f"/ws/conversations/{conversation_id}", headers=ORIGIN
    ) as ana_socket:
        owner_socket.send_json(
            {
                "type": "call_signal",
                "call_id": call["id"],
                "to_user_id": ana_id,
                "signal": "offer",
                "payload": {"type": "offer", "sdp": "v=0"},
            }
        )
        relayed = ana_socket.receive_json()
        assert relayed["type"] == "call_signal"
        assert relayed["signal"] == "offer"
        assert relayed["from_user_id"] == owner_id
        assert relayed["payload"] == {"type": "offer", "sdp": "v=0"}

        ana_socket.send_json(
            {
                "type": "call_signal",
                "call_id": call["id"],
                "to_user_id": owner_id,
                "signal": "answer",
                "payload": {"type": "answer", "sdp": "v=0"},
            }
        )
        assert owner_socket.receive_json()["signal"] == "answer"

        owner_socket.send_json(
            {
                "type": "call_signal",
                "call_id": call["id"],
                "to_user_id": ana_id,
                "signal": "ice",
                "payload": {
                    "candidate": {
                        "candidate": "candidate:1 1 UDP 1 192.0.2.7 5000 typ host",
                        "sdpMid": "0",
                    }
                },
            }
        )
        relayed_ice = ana_socket.receive_json()
        assert relayed_ice["signal"] == "ice"
        assert relayed_ice["payload"]["candidate"]["sdpMid"] == "0"

        # Ben was invited but never joined, so he cannot be signaled.
        owner_socket.send_json(
            {
                "type": "call_signal",
                "call_id": call["id"],
                "to_user_id": ben_id,
                "signal": "offer",
                "payload": {"type": "offer", "sdp": "v=0"},
            }
        )
        assert owner_socket.receive_json() == {
            "type": "call_error",
            "call_id": call["id"],
            "reason": "unavailable",
        }

        # Heartbeats and unreadable packets still leave the socket healthy.
        owner_socket.send_text("ping")
        assert owner_socket.receive_text() == "pong"
        owner_socket.send_text("not a signaling packet")
        owner_socket.send_json({"type": "call_signal", "call_id": "short"})

        assert owner.post(f"/calls/{call['id']}/end").status_code == 200
        # Ending the call pushes its final state to everyone in it first.
        assert owner_socket.receive_json()["call"]["state"] == "ended"
        owner_socket.send_json(
            {
                "type": "call_signal",
                "call_id": call["id"],
                "to_user_id": ana_id,
                "signal": "offer",
                "payload": {"type": "offer", "sdp": "v=0"},
            }
        )
        assert owner_socket.receive_json()["reason"] == "unavailable"


def test_signaling_relays_opaque_packets_and_persists_no_media(
    client_factory, database_factory
):
    """The capture test: what crosses the boundary is SDP it must not interpret or keep."""
    people = sign_up(client_factory, "alice", "bob")
    alice, bob = people["alice"], people["bob"]
    conversation_id = start_direct(alice, "bob")
    with database_factory() as database:
        alice_id = user_id(database, "alice")
        bob_id = user_id(database, "bob")

    call = ring(alice, conversation_id).json()
    assert bob.post(f"/calls/{call['id']}/accept").status_code == 200

    offer_sdp = (
        "v=0\r\n"
        "o=- 1 2 IN IP4 127.0.0.1\r\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        "a=fingerprint:sha-256 AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:"
        "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99\r\n"
    )
    candidate = {"candidate": "candidate:1 1 UDP 1 192.0.2.7 5000 typ host", "sdpMid": "0"}
    with alice.websocket_connect(
        f"/ws/conversations/{conversation_id}", headers=ORIGIN
    ) as alice_socket, bob.websocket_connect(
        f"/ws/conversations/{conversation_id}", headers=ORIGIN
    ) as bob_socket:
        alice_socket.send_json(
            {
                "type": "call_signal",
                "call_id": call["id"],
                "to_user_id": bob_id,
                "signal": "offer",
                "payload": {"type": "offer", "sdp": offer_sdp},
            }
        )
        relayed = bob_socket.receive_json()
        # Byte for byte: the relay adds routing metadata and nothing else.
        assert relayed["payload"] == {"type": "offer", "sdp": offer_sdp}

        bob_socket.send_json(
            {
                "type": "call_signal",
                "call_id": call["id"],
                "to_user_id": alice_id,
                "signal": "ice",
                "payload": {"candidate": candidate},
            }
        )
        assert alice_socket.receive_json()["payload"] == {"candidate": candidate}

        assert alice.post(f"/calls/{call['id']}/end").status_code == 200

    with database_factory() as database:
        record = database.get(Call, UUID(call["id"]))
        stored = " ".join(
            str(getattr(record, column.name)) for column in Call.__table__.columns
        )
        for row in database.scalars(select(CallParticipant)).all():
            stored += " " + " ".join(
                str(getattr(row, column.name)) for column in CallParticipant.__table__.columns
            )
        for event in database.scalars(select(OutboxEvent)).all():
            stored += " " + str(event.payload)
    for marker in ("fingerprint", "v=0", "candidate", "sdp"):
        assert marker not in stored.lower()


def test_a_signaling_flood_is_rate_limited(client_factory, database_factory):
    people = sign_up(client_factory, "alice", "bob")
    alice, bob = people["alice"], people["bob"]
    conversation_id = start_direct(alice, "bob")
    with database_factory() as database:
        bob_id = user_id(database, "bob")

    call = ring(alice, conversation_id).json()
    assert bob.post(f"/calls/{call['id']}/accept").status_code == 200
    limit = get_settings().call_signal_limit_per_minute
    packet = {
        "type": "call_signal",
        "call_id": call["id"],
        "to_user_id": bob_id,
        "signal": "ice",
        "payload": {"candidate": {"candidate": "candidate:1 1 UDP 1 192.0.2.7 5000 typ host"}},
    }
    with alice.websocket_connect(
        f"/ws/conversations/{conversation_id}", headers=ORIGIN
    ) as socket:
        for _ in range(limit):
            socket.send_json(packet)
        socket.send_json(packet)
        assert socket.receive_json()["reason"] == "rate-limited"


def test_call_records_are_private(client_factory):
    people = sign_up(client_factory, "alice", "bob", "outsider")
    alice, outsider = people["alice"], people["outsider"]
    conversation_id = start_direct(alice, "bob")
    call = ring(alice, conversation_id).json()

    assert outsider.get(f"/calls/{call['id']}").status_code == 404
    assert outsider.post(f"/calls/{call['id']}/accept").status_code == 404
    assert outsider.post(f"/calls/{call['id']}/end").status_code == 404
    assert outsider.get(f"/conversations/{conversation_id}").status_code == 404

    anonymous = client_factory()
    assert anonymous.get(f"/calls/{call['id']}").status_code == 401


def test_turn_credentials_are_short_lived(client_factory, database_factory, monkeypatch):
    people = sign_up(client_factory, "alice")
    alice = people["alice"]
    with database_factory() as database:
        alice_id = user_id(database, "alice")

    settings = get_settings().model_copy(
        update={
            "webrtc_turn_urls": "turn:relay.example.com:3478?transport=udp",
            "webrtc_turn_secret": "shared-secret",
            "webrtc_turn_ttl_seconds": 300,
        }
    )
    monkeypatch.setattr(calls_service, "get_settings", lambda: settings)

    before = int(time.time())
    response = alice.get("/calls/ice")
    assert response.status_code == 200
    servers = response.json()["ice_servers"]
    assert any("stun:" in str(server["urls"]) for server in servers)
    turn = next(server for server in servers if "turn:" in str(server["urls"]))
    expiry, _, subject = turn["username"].partition(":")
    assert subject == alice_id
    assert before + 295 <= int(expiry) <= int(time.time()) + 300
    credential = hmac.new(b"shared-secret", turn["username"].encode("utf-8"), hashlib.sha1)
    assert turn["credential"] == base64.b64encode(credential.digest()).decode("ascii")


def test_a_turn_url_without_a_secret_is_not_offered(client_factory, monkeypatch):
    people = sign_up(client_factory, "alice")
    alice = people["alice"]
    settings = get_settings().model_copy(
        update={"webrtc_turn_urls": "turn:relay.example.com:3478", "webrtc_turn_secret": None}
    )
    monkeypatch.setattr(calls_service, "get_settings", lambda: settings)
    servers = alice.get("/calls/ice").json()["ice_servers"]
    assert servers
    assert all("turn:" not in str(server["urls"]) for server in servers)


def test_a_closed_page_leaves_the_call(client_factory, database_factory):
    people = sign_up(client_factory, "alice", "bob")
    alice, bob = people["alice"], people["bob"]
    conversation_id = start_direct(alice, "bob")
    with database_factory() as database:
        bob_id = user_id(database, "bob")

    call = ring(bob, conversation_id).json()
    assert call["state"] == "ringing"

    with database_factory() as database:
        asyncio.run(
            calls_service.leave_call_on_disconnect(
                database, UUID(conversation_id), UUID(bob_id)
            )
        )
        record = database.get(Call, UUID(call["id"]))
        assert record.state == "ended"
        assert record.end_reason == "hangup"

    # Alice never answered, so the closed page leaves her a missed call.
    assert alice.get("/api/v1/notifications").json()["count"] == 1


def test_call_metrics_are_exported(client_factory):
    metrics.registry.reset()
    people = sign_up(client_factory, "alice", "bob")
    alice, bob = people["alice"], people["bob"]
    conversation_id = start_direct(alice, "bob")

    call = ring(alice, conversation_id).json()
    assert bob.post(f"/calls/{call['id']}/accept").status_code == 200
    assert alice.post(f"/calls/{call['id']}/end").status_code == 200

    body = alice.get("/metrics").text
    assert 'social_plus_calls_started_total{kind="direct",media="audio"} 1' in body
    assert 'social_plus_calls_ended_total{reason="hangup"} 1' in body
    assert "social_plus_call_ring_duration_seconds_count 1" in body
    assert "# TYPE social_plus_calls_active gauge" in body
    assert "# TYPE social_plus_call_participants_joined gauge" in body


def test_call_settings_round_trip(client_factory, database_factory):
    people = sign_up(client_factory, "alice")
    alice = people["alice"]

    assert (
        save_call_settings(alice, notify_calls="true").status_code == 303
    )
    with database_factory() as database:
        stored = database.scalar(select(User).where(User.username == "alice"))
        assert stored.notify_calls is True
        assert stored.accept_calls is False

    page = alice.get("/settings")
    assert 'name="accept_calls"' in page.text
    assert 'name="notify_calls"' in page.text
