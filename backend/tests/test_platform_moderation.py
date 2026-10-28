"""Platform moderation: the queue that answers a report about a personal update.

A personal update belongs to an account, so no community moderator can receive a report about
it. These tests hold three promises: only a moderator opens the queue or records a decision;
a report can only name content its reporter was allowed to read, once; and each of the four
decisions does what it says, in the audit log as well as in the page.
"""

from uuid import uuid4

from sqlalchemy import select

from app.core import events
from app.models import Comment, ContentReport, Notification, PlatformAuditEvent, User
from conftest import create_account, csrf_from


def register(client, username: str) -> None:
    assert create_account(client, f"{username}@example.com", username).status_code == 303


def publish(client, body: str = "Something worth reading.", audience: str = "public") -> str:
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


def post_id_of(location: str) -> str:
    return location.rsplit("/", 1)[-1]


def make_moderator(database_factory, username: str) -> None:
    with database_factory() as database:
        database.scalar(select(User).where(User.username == username)).is_moderator = True
        database.commit()


def report(
    client,
    location: str,
    *,
    target_type: str = "post",
    target_id: str | None = None,
    reason: str = "Spam.",
):
    """File a report through the form the post page renders."""
    post_id = post_id_of(location)
    return client.post(
        f"/posts/{post_id}/reports/{target_type}/{target_id or post_id}",
        data={"csrf_token": csrf_from(client.get("/posts/new").text), "reason": reason},
        follow_redirects=False,
    )


def resolve(client, report_id, decision: str):
    return client.post(
        f"/moderation/reports/{report_id}/resolve",
        data={"csrf_token": csrf_from(client.get("/posts/new").text), "decision": decision},
        follow_redirects=False,
    )


def stored_reports(database_factory) -> list[dict]:
    with database_factory() as database:
        rows = database.scalars(
            select(ContentReport)
            .where(ContentReport.community_id.is_(None))
            .order_by(ContentReport.created_at)
        ).all()
        return [
            {
                "id": row.id,
                "status": row.status,
                "target_type": row.target_type,
                "reason": row.reason,
            }
            for row in rows
        ]


def a_moderator(client_factory, database_factory):
    moderator = client_factory()
    register(moderator, "moderator")
    make_moderator(database_factory, "moderator")
    return moderator


def test_only_a_moderator_reaches_the_queue(client_factory, database_factory):
    member = client_factory()
    anonymous = client_factory()
    register(member, "member")

    assert anonymous.get("/moderation", follow_redirects=False).status_code == 303
    assert member.get("/moderation").status_code == 403

    assert a_moderator(client_factory, database_factory).get("/moderation").status_code == 200


def test_a_report_about_a_public_update_lands_in_the_platform_queue(
    client_factory, database_factory
):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    moderator = a_moderator(client_factory, database_factory)

    location = publish(author, "The web can read this.")
    assert report(reader, location).status_code == 303

    stored = stored_reports(database_factory)
    assert len(stored) == 1
    assert stored[0]["status"] == "open"
    assert stored[0]["reason"] == "Spam."

    queue = moderator.get("/moderation")
    assert queue.status_code == 200
    assert "Spam." in queue.text
    assert "The web can read this." in queue.text


def test_a_report_needs_readable_content_that_is_not_your_own(client_factory, database_factory):
    author = client_factory()
    reader = client_factory()
    stranger = client_factory()
    register(author, "author")
    register(reader, "reader")
    register(stranger, "stranger")

    followers_only = publish(author, "For followers.", audience="followers")
    # A reader the audience keeps out cannot name the post, and learns nothing by asking.
    assert report(stranger, followers_only).status_code == 404
    # Reporting your own post is refused for the same reason the report button is not drawn.
    assert report(author, publish(author, "Mine.", audience="public")).status_code == 404

    public = publish(author, "Open to the web.", audience="public")
    blank = report(reader, public, reason="   ")
    assert blank.headers["location"].endswith("error=invalid-report")
    assert stored_reports(database_factory) == []


def test_the_same_reader_reports_a_post_once(client_factory, database_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    location = publish(author)

    assert report(reader, location).status_code == 303
    duplicate = report(reader, location)

    assert duplicate.headers["location"].endswith("error=already-reported")
    assert len(stored_reports(database_factory)) == 1


def test_an_anonymous_reader_cannot_report(client_factory, database_factory):
    author = client_factory()
    register(author, "author")
    location = publish(author)
    post_id = post_id_of(location)

    anonymous = client_factory()
    response = anonymous.post(
        f"/posts/{post_id}/reports/post/{post_id}",
        data={"csrf_token": "not-a-session", "reason": "Spam."},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert stored_reports(database_factory) == []


def test_a_non_moderator_cannot_record_a_decision(client_factory, database_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    report(reader, publish(author))
    report_id = stored_reports(database_factory)[0]["id"]

    assert resolve(reader, report_id, "remove").status_code == 403
    assert stored_reports(database_factory)[0]["status"] == "open"


def test_a_moderator_removes_a_reported_update(client_factory, database_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    moderator = a_moderator(client_factory, database_factory)
    location = publish(author, "Not acceptable.")
    report(reader, location, reason="Harassment.")
    report_id = stored_reports(database_factory)[0]["id"]
    assert client_factory().get(location).status_code == 200

    assert resolve(moderator, report_id, "remove").status_code == 303

    # The open web loses it, the author keeps a tombstone, and the trail says who decided.
    assert client_factory().get(location).status_code == 404
    assert "This update was removed." in author.get(location).text
    assert stored_reports(database_factory)[0]["status"] == "resolved"
    with database_factory() as database:
        actions = {row.action for row in database.scalars(select(PlatformAuditEvent)).all()}
        assert events.POST_REMOVED in actions
        assert events.REPORT_RESOLVED in actions
        author_row = database.scalar(select(User).where(User.username == "author"))
        notification = database.scalars(
            select(Notification).where(Notification.recipient_user_id == author_row.id)
        ).first()
        assert notification is not None


def test_dismissing_leaves_the_update_readable(client_factory, database_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    moderator = a_moderator(client_factory, database_factory)
    location = publish(author, "Nothing wrong here.")
    report(reader, location)
    report_id = stored_reports(database_factory)[0]["id"]

    assert resolve(moderator, report_id, "dismiss").status_code == 303

    assert client_factory().get(location).status_code == 200
    assert stored_reports(database_factory)[0]["status"] == "dismissed"
    with database_factory() as database:
        actions = {row.action for row in database.scalars(select(PlatformAuditEvent)).all()}
        assert events.REPORT_DISMISSED in actions


def test_locking_closes_the_thread(client_factory, database_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    moderator = a_moderator(client_factory, database_factory)
    location = publish(author, "Worth discussing.")
    post_id = post_id_of(location)
    reply = reader.post(
        f"/posts/{post_id}/comments",
        data={"csrf_token": csrf_from(reader.get("/posts/new").text), "body": "A reply."},
        follow_redirects=False,
    )
    assert reply.status_code == 303
    report(reader, location)
    report_id = stored_reports(database_factory)[0]["id"]

    assert resolve(moderator, report_id, "lock").status_code == 303

    assert client_factory().get(location).status_code == 200
    blocked = reader.post(
        f"/posts/{post_id}/comments",
        data={"csrf_token": csrf_from(reader.get("/posts/new").text), "body": "Another."},
        follow_redirects=False,
    )
    assert blocked.status_code == 409
    page = reader.get(location).text
    assert "A moderator locked this discussion." in page
    assert 'class="comment-compose"' not in page


def test_suspending_ends_the_account(client_factory, database_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    moderator = a_moderator(client_factory, database_factory)
    location = publish(author, "From an account that will end.")
    report(reader, location, reason="Impersonation.")
    report_id = stored_reports(database_factory)[0]["id"]

    assert resolve(moderator, report_id, "suspend").status_code == 303

    # The author's session stops working and the public page goes with the account.
    assert author.get("/profile", follow_redirects=False).status_code == 303
    assert client_factory().get(location).status_code == 404
    with database_factory() as database:
        author_row = database.scalar(select(User).where(User.username == "author"))
        assert author_row.is_active is False
        actions = {row.action for row in database.scalars(select(PlatformAuditEvent)).all()}
        assert events.ACCOUNT_SUSPENDED in actions


def test_a_comment_can_be_reported_and_removed(client_factory, database_factory):
    author = client_factory()
    reader = client_factory()
    register(author, "author")
    register(reader, "reader")
    moderator = a_moderator(client_factory, database_factory)
    location = publish(author, "The thread is the point.")
    post_id = post_id_of(location)
    # The author writes the reply; the reader reports it, since nobody reports their own words.
    author.post(
        f"/posts/{post_id}/comments",
        data={"csrf_token": csrf_from(author.get("/posts/new").text), "body": "A rude reply."},
        follow_redirects=False,
    )
    with database_factory() as database:
        comment_id = database.scalars(select(Comment.id)).one()

    assert report(reader, location, target_type="comment", target_id=comment_id).status_code == 303
    report_id = stored_reports(database_factory)[0]["id"]
    assert resolve(moderator, report_id, "remove").status_code == 303

    page = author.get(location)
    assert page.status_code == 200
    assert "A rude reply." not in page.text


def test_the_platform_queue_ignores_a_community_report(client_factory, database_factory):
    moderator = a_moderator(client_factory, database_factory)
    with database_factory() as database:
        author_row = database.scalar(select(User).where(User.username == "moderator"))
        community_report = ContentReport(
            community_id=uuid4(),
            reporter_id=author_row.id,
            target_type="post",
            target_id=uuid4(),
            reason="A community matter.",
        )
        database.add(community_report)
        database.commit()
        report_id = community_report.id

    # The community queue owns this one; the platform route must not reach across.
    assert resolve(moderator, report_id, "dismiss").status_code == 404
    assert "A community matter." not in moderator.get("/moderation").text
