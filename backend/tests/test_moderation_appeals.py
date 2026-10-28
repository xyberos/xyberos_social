"""Appeals: the author's one chance to question a removal the platform queue decided.

The rules these tests hold: only the content's author may appeal, only a platform removal is
appealable (not a community moderator's decision and not the author's own withdrawal), an appeal
is filed once, and granting it really restores the content to the open web while keeping the
decision in the audit log.
"""

from sqlalchemy import select

from app.core import events
from app.models import ModerationAppeal, Notification, OutboxEvent, PlatformAuditEvent, User
from conftest import csrf_from
from test_platform_moderation import (
    a_moderator,
    post_id_of,
    publish,
    register,
    report,
    resolve,
    stored_reports,
)


def appeal(client, location: str, reason: str = "It was a mistake."):
    post_id = post_id_of(location)
    return client.post(
        f"/posts/{post_id}/appeals/post/{post_id}",
        data={"csrf_token": csrf_from(client.get("/posts/new").text), "reason": reason},
        follow_redirects=False,
    )


def decide(client, appeal_id, decision: str):
    return client.post(
        f"/moderation/appeals/{appeal_id}/resolve",
        data={"csrf_token": csrf_from(client.get("/posts/new").text), "decision": decision},
        follow_redirects=False,
    )


def stored_appeals(database_factory) -> list[dict]:
    with database_factory() as database:
        rows = database.scalars(select(ModerationAppeal).order_by(ModerationAppeal.created_at)).all()
        return [{"id": row.id, "status": row.status, "reason": row.reason} for row in rows]


def removed_by_a_moderator(client_factory, database_factory):
    """An author, a reporter, a moderator, and one public update the moderator removed."""
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    moderator = a_moderator(client_factory, database_factory)
    location = publish(author, "Removed, then questioned.")
    report(reader, location, reason="Off-topic.")
    assert resolve(moderator, stored_reports(database_factory)[0]["id"], "remove").status_code == 303
    assert client_factory().get(location).status_code == 404
    return author, reader, moderator, location


def test_an_author_appeals_and_a_moderator_restores_the_update(
    client_factory, database_factory
):
    author, _reader, moderator, location = removed_by_a_moderator(
        client_factory, database_factory
    )

    # The author's own view offers the appeal; nobody else's does.
    page = author.get(location)
    assert page.status_code == 200
    assert "Appeal this removal" in page.text

    assert appeal(author, location).status_code == 303
    stored = stored_appeals(database_factory)
    assert len(stored) == 1
    assert stored[0]["status"] == "pending"

    queue = moderator.get("/moderation")
    assert "Appeals awaiting review" in queue.text
    assert "It was a mistake." in queue.text

    assert decide(moderator, stored[0]["id"], "grant").status_code == 303

    # Restored for the open web, and the decision that restored it is in the log.
    assert client_factory().get(location).status_code == 200
    assert stored_appeals(database_factory)[0]["status"] == "granted"
    with database_factory() as database:
        actions = {row.action for row in database.scalars(select(PlatformAuditEvent)).all()}
        outbox = {row.event for row in database.scalars(select(OutboxEvent)).all()}
        # Filing is not a privileged change, so it lives in the outbox; the decision is audited.
        assert events.MODERATION_APPEAL_FILED in outbox
        assert events.MODERATION_APPEAL_GRANTED in actions
        author_row = database.scalar(select(User).where(User.username == "author"))
        notification = database.scalars(
            select(Notification).where(Notification.recipient_user_id == author_row.id)
        ).first()
        assert notification is not None
    assert "Removed, then questioned." in author.get(location).text


def test_only_the_author_may_appeal(client_factory, database_factory):
    _author, reader, _moderator, location = removed_by_a_moderator(
        client_factory, database_factory
    )

    # The reader cannot even see the removed update, so the appeal is the same 404.
    assert appeal(reader, location).status_code == 404
    assert stored_appeals(database_factory) == []


def test_a_self_withdrawal_is_not_appealable(client_factory, database_factory):
    author = client_factory()
    register(author, "author")
    location = publish(author, "Withdrawn by its author.")
    post_id = post_id_of(location)

    withdrawn = author.post(
        f"/posts/{post_id}/remove",
        data={"csrf_token": csrf_from(author.get("/posts/new").text)},
        follow_redirects=False,
    )
    assert withdrawn.status_code == 303

    # No moderator decided this, so there is nothing for the queue to review.
    assert appeal(author, location).status_code == 404
    assert stored_appeals(database_factory) == []


def test_an_appeal_is_filed_once(client_factory, database_factory):
    author, _reader, _moderator, location = removed_by_a_moderator(
        client_factory, database_factory
    )

    assert appeal(author, location).status_code == 303
    duplicate = appeal(author, location)

    assert duplicate.headers["location"].endswith("error=already-appealed")
    assert len(stored_appeals(database_factory)) == 1


def test_denying_leaves_the_removal_in_place(client_factory, database_factory):
    author, _reader, moderator, location = removed_by_a_moderator(
        client_factory, database_factory
    )
    assert appeal(author, location).status_code == 303
    appeal_id = stored_appeals(database_factory)[0]["id"]

    assert decide(moderator, appeal_id, "deny").status_code == 303

    assert client_factory().get(location).status_code == 404
    assert stored_appeals(database_factory)[0]["status"] == "denied"
    assert "Appeal denied" in author.get(location).text
    with database_factory() as database:
        actions = {row.action for row in database.scalars(select(PlatformAuditEvent)).all()}
        assert events.MODERATION_APPEAL_DENIED in actions


def test_a_non_moderator_cannot_decide_an_appeal(client_factory, database_factory):
    author, reader, _moderator, location = removed_by_a_moderator(
        client_factory, database_factory
    )
    assert appeal(author, location).status_code == 303
    appeal_id = stored_appeals(database_factory)[0]["id"]

    assert decide(reader, appeal_id, "grant").status_code == 403
    assert stored_appeals(database_factory)[0]["status"] == "pending"
