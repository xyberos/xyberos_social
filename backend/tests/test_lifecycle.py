import html
import re

from conftest import create_account, csrf_from


def create_community(client, slug: str) -> None:
    page = client.get("/communities/new")
    response = client.post(
        "/communities",
        data={
            "csrf_token": csrf_from(page.text),
            "name": slug.replace("-", " ").title(),
            "slug": slug,
            "description": "A community for a focused discussion.",
            "visibility": "public",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def create_post(client, slug: str, title: str) -> str:
    page = client.get(f"/communities/{slug}")
    response = client.post(
        f"/communities/{slug}/posts",
        data={"csrf_token": csrf_from(page.text), "title": title, "body": "Context."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def join_community(client, slug: str) -> None:
    page = client.get(f"/communities/{slug}")
    response = client.post(
        f"/communities/{slug}/join",
        data={"csrf_token": csrf_from(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def reply(client, slug: str, post_id: str, body: str) -> None:
    page = client.get(f"/communities/{slug}/posts/{post_id}")
    response = client.post(
        f"/communities/{slug}/posts/{post_id}/comments",
        data={"csrf_token": csrf_from(page.text), "body": body},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_reply_notifications_preferences_and_finite_api(client_factory):
    author = client_factory()
    member = client_factory()
    assert create_account(author, "author@example.com", "author").status_code == 303
    assert create_account(member, "member@example.com", "member").status_code == 303
    create_community(author, "notification-room")
    post_id = create_post(author, "notification-room", "Reply target")
    join_community(member, "notification-room")

    reply(member, "notification-room", post_id, "First reply.")
    reply(member, "notification-room", post_id, "Second reply.")
    first_batch = author.get("/api/v1/notifications?limit=1")
    assert first_batch.status_code == 200
    first_payload = first_batch.json()
    assert first_payload["count"] == 1
    assert first_payload["has_more"] is True
    assert first_payload["notifications"][0]["category"] == "reply"
    assert first_payload["notifications"][0]["target_url"].endswith(post_id)
    next_payload = author.get(
        "/api/v1/notifications?limit=1&cursor=" + html.escape(first_payload["next_cursor"])
    ).json()
    assert next_payload["notifications"][0]["id"] != first_payload["notifications"][0]["id"]
    assert author.get("/api/v1/notifications?limit=51").status_code == 422
    assert member.get("/api/v1/notifications").status_code == 200

    settings = author.get("/settings")
    saved = author.post(
        "/settings",
        data={
            "csrf_token": csrf_from(settings.text),
            "updates_source": "all",
            "updates_days": "7",
            "updates_limit": "20",
            "notify_moderation": "true",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    reply(member, "notification-room", post_id, "Muted reply.")
    assert author.get("/api/v1/notifications").json()["count"] == 2

    settings = author.get("/settings")
    author.post(
        "/settings",
        data={
            "csrf_token": csrf_from(settings.text),
            "updates_source": "all",
            "updates_days": "7",
            "updates_limit": "20",
            "quiet_mode": "true",
            "notify_replies": "true",
        },
        follow_redirects=False,
    )
    reply(member, "notification-room", post_id, "Quiet-mode reply.")
    assert author.get("/api/v1/notifications").json()["count"] == 2

    inbox = author.get("/notifications")
    assert "2 unread" in inbox.text
    marked = author.post(
        "/notifications/read-all",
        data={"csrf_token": csrf_from(inbox.text)},
        follow_redirects=False,
    )
    assert marked.status_code == 303
    assert author.get("/api/v1/notifications").json()["notifications"][0]["read_at"]
    inbox = author.get("/notifications")
    assert author.post(
        "/notifications/clear",
        data={"csrf_token": csrf_from(inbox.text)},
        follow_redirects=False,
    ).status_code == 303
    assert author.get("/api/v1/notifications").json()["count"] == 0
    assert author.get("/account/export").headers["content-disposition"].startswith("attachment;")


def test_moderation_outcome_is_notified_and_export_contains_user_data(client_factory):
    owner = client_factory()
    reporter = client_factory()
    assert create_account(owner, "owner@example.com", "owner").status_code == 303
    assert create_account(reporter, "reporter@example.com", "reporter").status_code == 303
    create_community(owner, "report-notification-room")
    post_id = create_post(owner, "report-notification-room", "Reported topic")
    report_page = reporter.get("/profile")
    report = reporter.post(
        f"/communities/report-notification-room/reports/post/{post_id}",
        data={"csrf_token": csrf_from(report_page.text), "reason": "Needs a review."},
        follow_redirects=False,
    )
    assert report.status_code == 303
    queue = owner.get("/communities/report-notification-room/reports")
    report_id = re.search(r"/report-queue/([0-9a-f-]+)/resolve", queue.text).group(1)
    resolved = owner.post(
        f"/communities/report-notification-room/report-queue/{report_id}/resolve",
        data={"csrf_token": csrf_from(queue.text), "decision": "dismiss"},
        follow_redirects=False,
    )
    assert resolved.status_code == 303
    notification = reporter.get("/api/v1/notifications").json()["notifications"][0]
    assert notification["category"] == "moderation"
    assert "dismissed" in notification["message"]

    exported = reporter.get("/account/export")
    payload = exported.json()
    assert payload["reports"][0]["reason"] == "Needs a review."
    assert payload["account"]["updates_source"] == "all"
    assert payload["account"]["updates_days"] == 7
    assert payload["account"]["updates_limit"] == 20
    assert "password_hash" not in payload["account"]
    assert "sessions" not in payload


def test_account_deletion_requires_confirmation_and_preserves_shared_history(client_factory):
    owner = client_factory()
    member = client_factory()
    assert create_account(owner, "owner@example.com", "owner").status_code == 303
    assert create_account(member, "member@example.com", "member").status_code == 303
    create_community(owner, "handoff-room")
    create_community(owner, "owner-only-room")
    post_id = create_post(owner, "handoff-room", "Retained discussion")
    join_community(member, "handoff-room")
    profile = owner.get("/profile")
    csrf_token = csrf_from(profile.text)

    wrong_confirmation = owner.post(
        "/account/delete",
        data={
            "csrf_token": csrf_token,
            "password": "correct-horse-battery-staple",
            "confirmation": "delete",
        },
        follow_redirects=False,
    )
    assert wrong_confirmation.status_code == 422
    wrong_password = owner.post(
        "/account/delete",
        data={"csrf_token": csrf_token, "password": "incorrect-password", "confirmation": "DELETE"},
        follow_redirects=False,
    )
    assert wrong_password.status_code == 403
    assert owner.get("/profile").status_code == 200

    deleted = owner.post(
        "/account/delete",
        data={
            "csrf_token": csrf_token,
            "password": "correct-horse-battery-staple",
            "confirmation": "DELETE",
        },
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/account-deleted"
    assert "your role: owner" in member.get("/communities/handoff-room").text
    assert member.get(f"/communities/handoff-room/posts/{post_id}").text.count("Deleted account") > 0
    assert "owner-only-room" not in member.get("/communities").text
    assert owner.get("/profile", follow_redirects=False).headers["location"] == "/login"
    assert "Backups may retain" in owner.get("/account-deleted").text