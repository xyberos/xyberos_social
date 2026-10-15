"""The bridge layer: platform audit log, transactional outbox, idempotency, and scoping."""

import asyncio
import json
import re
from uuid import uuid4

import pytest
from fastapi import Request
from sqlalchemy import func, select

from conftest import create_account, csrf_from

from app.api.middleware import scope_violation_response
from app.core import events
from app.core.scoping import (
    Scope,
    ScopeViolation,
    account_scope,
    business_scope,
    community_scope,
    platform_scope,
    scoped_get,
)
from app.models import (
    Community,
    CommunityAuditEvent,
    ConversationMessage,
    IdempotencyKey,
    OutboxEvent,
    PlatformAuditEvent,
    Post,
    User,
)
from app.services.audit import audit_trail
from app.services.communities import delete_unowned_community
from app.services.outbox import enqueue_event

CHAT_HEADERS = {"X-Chat-Request": "true"}


def sign_in(client_factory, username: str):
    client = client_factory()
    assert create_account(client, f"{username}@example.com", username).status_code == 303
    return client


def create_community(client, slug: str, name: str):
    page = client.get("/communities/new")
    return client.post(
        "/communities",
        data={
            "csrf_token": csrf_from(page.text),
            "name": name,
            "slug": slug,
            "description": "",
            "visibility": "public",
        },
        follow_redirects=False,
    )


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


def test_event_vocabulary_is_one_namespace():
    for name in events.SOCIAL_EVENTS + events.COMMERCIAL_EVENTS:
        assert events.is_event_name(name), name
        assert events.domain_of(name) == name.split(".", 1)[0]
    assert events.COMMERCIAL_EVENTS  # reserved before the phases that emit them

    assert events.validate_event_name("offering.published") == "offering.published"
    for unusable in ["", "created", "communityCreated", "Community.created", ".created", "a."]:
        with pytest.raises(ValueError):
            events.validate_event_name(unusable)


def test_community_creation_records_audit_outbox_and_typed_history(client_factory, database_factory):
    alice = sign_in(client_factory, "alice")
    response = create_community(alice, "bridge-lab", "Bridge Lab")
    assert response.status_code == 303

    with database_factory() as database:
        community = database.scalar(select(Community).where(Community.slug == "bridge-lab"))
        audit = database.scalar(select(PlatformAuditEvent))
        assert audit.action == events.COMMUNITY_CREATED
        assert audit.scope_type == "community"
        assert audit.scope_id == community.id
        assert audit.target_id == community.id
        assert audit.request_id == response.headers["x-request-id"]
        assert audit.after_state == {
            "name": "Bridge Lab",
            "slug": "bridge-lab",
            "visibility": "public",
        }

        outbox = database.scalar(select(OutboxEvent))
        assert outbox.event == events.COMMUNITY_CREATED
        assert outbox.aggregate_type == "community"
        assert outbox.aggregate_id == community.id
        assert outbox.published_at is None

        typed = database.scalars(
            select(CommunityAuditEvent).where(CommunityAuditEvent.community_id == community.id)
        ).all()
        assert [row.action for row in typed] == ["community_created"]


def test_role_change_records_before_and_after_state(client_factory, database_factory):
    alice = sign_in(client_factory, "alice")
    bob = sign_in(client_factory, "bob")
    assert create_community(alice, "roles", "Roles").status_code == 303
    joined = bob.get("/communities/roles")
    assert (
        bob.post(
            "/communities/roles/join",
            data={"csrf_token": csrf_from(joined.text)},
            follow_redirects=False,
        ).status_code
        == 303
    )
    with database_factory() as database:
        community_id = database.scalar(select(Community.id).where(Community.slug == "roles"))
        bob_id = database.scalar(select(User.id).where(User.username == "bob"))

    page = alice.get("/communities/roles")
    promoted = alice.post(
        f"/communities/roles/members/{bob_id}/role",
        data={"csrf_token": csrf_from(page.text), "role": "moderator"},
        follow_redirects=False,
    )
    assert promoted.status_code == 303

    with database_factory() as database:
        audit = database.scalar(
            select(PlatformAuditEvent).where(
                PlatformAuditEvent.action == events.COMMUNITY_ROLE_CHANGED
            )
        )
        assert audit.scope_id == community_id
        assert audit.target_id == bob_id
        assert audit.before_state == {"role": "member"}
        assert audit.after_state == {"role": "moderator"}


def test_post_creation_enqueues_a_scoped_outbox_event(client_factory, database_factory):
    alice = sign_in(client_factory, "alice")
    assert create_community(alice, "outbox", "Outbox").status_code == 303
    page = alice.get("/communities/outbox")
    created = alice.post(
        "/communities/outbox/posts",
        data={"csrf_token": csrf_from(page.text), "title": "First", "body": "Body"},
        follow_redirects=False,
    )
    assert created.status_code == 303

    with database_factory() as database:
        post = database.scalar(select(Post).where(Post.title == "First"))
        outbox = database.scalar(
            select(OutboxEvent).where(OutboxEvent.event == events.POST_CREATED)
        )
        assert outbox.aggregate_id == post.id
        assert outbox.aggregate_type == "post"
        assert outbox.scope_type == "community"
        assert outbox.scope_id == post.community_id
        assert outbox.published_at is None


def test_outbox_event_rolls_back_with_its_state_change(database_factory):
    with database_factory() as database:
        user = User(
            email="rollback@example.com",
            username="rollback",
            display_name="Rollback",
            password_hash="not-a-real-hash",
        )
        database.add(user)
        database.flush()
        community = Community(name="Rollback", slug="rollback", created_by=user.id)
        database.add(community)
        database.flush()
        post = Post(community_id=community.id, author_id=user.id, title="Gone", body="")
        database.add(post)
        database.flush()
        enqueue_event(
            database,
            event=events.POST_CREATED,
            aggregate_type="post",
            aggregate_id=post.id,
            scope=community_scope(community.id),
        )
        database.flush()
        assert database.scalar(select(func.count()).select_from(OutboxEvent)) == 1

        database.rollback()

    with database_factory() as database:
        assert database.scalar(select(func.count()).select_from(Post)) == 0
        assert database.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_retried_message_replays_the_first_outcome(client_factory, database_factory):
    alice = sign_in(client_factory, "alice")
    bob = sign_in(client_factory, "bob")
    conversation_id = start_direct(alice, "bob")
    url = f"/conversations/{conversation_id}/messages"
    data = {
        "csrf_token": csrf_from(alice.get(f"/conversations/{conversation_id}").text),
        "body": "Only once",
        "idempotency_key": "retry-key-0001",
    }

    first = alice.post(url, data=data, headers=CHAT_HEADERS)
    second = alice.post(url, data=data, headers=CHAT_HEADERS)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    conflict = alice.post(url, data={**data, "body": "Different text"}, headers=CHAT_HEADERS)
    assert conflict.status_code == 409
    assert "already used" in conflict.json()["detail"]

    unusable = alice.post(url, data={**data, "idempotency_key": "short"}, headers=CHAT_HEADERS)
    assert unusable.status_code == 400

    bob_detail = bob.get(f"/conversations/{conversation_id}")
    other_actor = bob.post(
        url,
        data={
            "csrf_token": csrf_from(bob_detail.text),
            "body": "Only once",
            "idempotency_key": "retry-key-0001",
        },
        headers=CHAT_HEADERS,
    )
    assert other_actor.status_code == 200
    assert other_actor.json()["id"] != first.json()["id"]

    with database_factory() as database:
        assert database.scalar(select(func.count()).select_from(ConversationMessage)) == 2
        assert (
            database.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event == events.MESSAGE_CREATED)
            )
            == 2
        )
        record = database.scalar(
            select(IdempotencyKey).where(IdempotencyKey.key == "retry-key-0001")
        )
        assert record.state == "completed"
        assert record.response_status == 200


def test_form_retry_redirects_instead_of_duplicating(client_factory, database_factory):
    alice = sign_in(client_factory, "alice")
    sign_in(client_factory, "bob")
    conversation_id = start_direct(alice, "bob")
    url = f"/conversations/{conversation_id}/messages"
    data = {
        "csrf_token": csrf_from(alice.get(f"/conversations/{conversation_id}").text),
        "body": "Form once",
        "idempotency_key": "form-key-0001",
    }

    first = alice.post(url, data=data, follow_redirects=False)
    second = alice.post(url, data=data, follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert first.headers["location"] == f"/conversations/{conversation_id}"
    assert second.headers["location"] == first.headers["location"]

    with database_factory() as database:
        assert database.scalar(select(func.count()).select_from(ConversationMessage)) == 1


def test_scope_predicates_fail_closed(client_factory, database_factory):
    alice = sign_in(client_factory, "alice")
    assert create_community(alice, "scope-one", "Scope One").status_code == 303
    assert create_community(alice, "scope-two", "Scope Two").status_code == 303

    with database_factory() as database:
        first = database.scalar(select(Community).where(Community.slug == "scope-one"))
        second = database.scalar(select(Community).where(Community.slug == "scope-two"))
        trail = audit_trail(database, community_scope(first.id))
        assert [row.scope_id for row in trail] == [first.id]
        assert audit_trail(database, community_scope(uuid4())) == []

        row = database.scalar(
            select(PlatformAuditEvent).where(PlatformAuditEvent.scope_id == first.id)
        )
        assert scoped_get(database, PlatformAuditEvent, row.id, community_scope(first.id)).id == (
            row.id
        )
        with pytest.raises(ScopeViolation):
            scoped_get(database, PlatformAuditEvent, row.id, community_scope(second.id))


def test_scope_rejects_impossible_values():
    with pytest.raises(ValueError):
        Scope("platform", uuid4())
    with pytest.raises(ValueError):
        Scope("community")
    with pytest.raises(ValueError):
        Scope("not-a-scope", uuid4())

    assert platform_scope().columns() == {"scope_type": "platform", "scope_id": None}
    assert business_scope(uuid4()).kind == "business"
    assert str(account_scope(uuid4())).startswith("account:")


def test_scope_violation_answers_like_a_missing_row():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/communities/other",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("127.0.0.1", 1234),
            "state": {"request_id": "trace-abcdef12"},
        }
    )
    violation = ScopeViolation(community_scope(uuid4()), account_scope(uuid4()))

    response = asyncio.run(scope_violation_response(request, violation))

    assert response.status_code == 404
    assert json.loads(response.body) == {"detail": "Not found.", "request_id": "trace-abcdef12"}


def test_deleting_a_community_removes_its_scoped_records(client_factory, database_factory):
    alice = sign_in(client_factory, "alice")
    assert create_community(alice, "disposable", "Disposable").status_code == 303

    with database_factory() as database:
        community = database.scalar(select(Community).where(Community.slug == "disposable"))
        community_id = community.id
        delete_unowned_community(database, community)
        database.commit()

    with database_factory() as database:
        scoped_audits = database.scalars(
            select(PlatformAuditEvent).where(PlatformAuditEvent.scope_id == community_id)
        ).all()
        assert scoped_audits == []
        typed = database.scalars(
            select(CommunityAuditEvent).where(CommunityAuditEvent.community_id == community_id)
        ).all()
        assert typed == []
        surviving = database.scalars(
            select(OutboxEvent).where(OutboxEvent.scope_type == "platform")
        ).all()
        assert [row.event for row in surviving] == [events.COMMUNITY_DELETED]
        platform_audit = database.scalar(
            select(PlatformAuditEvent).where(PlatformAuditEvent.scope_type == "platform")
        )
        assert platform_audit.action == events.COMMUNITY_DELETED
        assert platform_audit.target_id == community_id
