import re

from conftest import create_account, csrf_from


def create_community(client, slug: str):
    page = client.get("/communities/new")
    response = client.post(
        "/communities",
        data={
            "csrf_token": csrf_from(page.text),
            "name": slug.replace("-", " ").title(),
            "slug": slug,
            "description": "A place for a focused discussion.",
            "visibility": "public",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def create_post(client, slug: str, title: str = "A thoughtful topic") -> str:
    page = client.get(f"/communities/{slug}")
    response = client.post(
        f"/communities/{slug}/posts",
        data={"csrf_token": csrf_from(page.text), "title": title, "body": "Some useful context."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


def join_community(client, slug: str):
    page = client.get(f"/communities/{slug}")
    return client.post(
        f"/communities/{slug}/join",
        data={"csrf_token": csrf_from(page.text)},
        follow_redirects=False,
    )


def test_community_page_shows_the_composer_and_its_posts(client_factory):
    owner = client_factory()
    member = client_factory()
    outsider = client_factory()
    assert create_account(owner, "owner@example.com", "owner").status_code == 303
    assert create_account(member, "member@example.com", "member").status_code == 303
    assert create_account(outsider, "outsider@example.com", "outsider").status_code == 303
    create_community(owner, "composer-lab")
    assert join_community(member, "composer-lab").status_code == 303
    create_post(owner, "composer-lab", "First topic")

    owner_page = owner.get("/communities/composer-lab")
    assert 'action="/communities/composer-lab/posts"' in owner_page.text
    assert "First topic" in owner_page.text

    # A plain member sees the composer too, and their post appears for everyone else.
    member_page = member.get("/communities/composer-lab")
    assert 'action="/communities/composer-lab/posts"' in member_page.text
    member_post = member.post(
        "/communities/composer-lab/posts",
        data={"csrf_token": csrf_from(member_page.text), "title": "Member topic", "body": ""},
        follow_redirects=False,
    )
    assert member_post.status_code == 303
    assert "Member topic" in owner.get("/communities/composer-lab").text

    # A signed-in non-member is told to join rather than shown a form that would refuse them.
    outsider_page = outsider.get("/communities/composer-lab")
    assert 'action="/communities/composer-lab/posts"' not in outsider_page.text
    assert "Join this community to start a discussion." in outsider_page.text


def test_posts_nested_comments_membership_and_moderator_lock(client_factory):
    owner = client_factory()
    member = client_factory()
    outsider = client_factory()
    for client, username in [(owner, "owner"), (member, "member"), (outsider, "outside")]:
        assert create_account(client, f"{username}@example.com", username).status_code == 303
    create_community(owner, "conversation-room")
    assert join_community(member, "conversation-room").status_code == 303
    post_id = create_post(owner, "conversation-room")
    post_url = f"/communities/conversation-room/posts/{post_id}"
    community_page = owner.get("/communities/conversation-room")
    duplicate = owner.post(
        "/communities/conversation-room/posts",
        data={
            "csrf_token": csrf_from(community_page.text),
            "title": "A thoughtful topic",
            "body": "Some useful context.",
        },
        follow_redirects=False,
    )
    assert "duplicate-post" in duplicate.headers["location"]

    public_read = outsider.get(post_url)
    assert public_read.status_code == 200
    outsider_profile = outsider.get("/profile")
    denied_comment = outsider.post(
        f"/communities/conversation-room/posts/{post_id}/comments",
        data={"csrf_token": csrf_from(outsider_profile.text), "body": "I am not a member."},
    )
    assert denied_comment.status_code == 403

    member_page = member.get(post_url)
    root_reply = member.post(
        f"/communities/conversation-room/posts/{post_id}/comments",
        data={"csrf_token": csrf_from(member_page.text), "body": "A useful reply."},
        follow_redirects=False,
    )
    assert root_reply.status_code == 303
    thread = member.get(post_url)
    root_id = re.search(r'id="comment-([0-9a-f-]+)"', thread.text).group(1)
    child_reply = member.post(
        f"/communities/conversation-room/posts/{post_id}/comments",
        data={
            "csrf_token": csrf_from(thread.text),
            "parent_id": root_id,
            "body": "A nested reply.",
        },
        follow_redirects=False,
    )
    assert child_reply.status_code == 303
    assert "A nested reply." in member.get(post_url).text

    owner_page = owner.get(post_url)
    lock = owner.post(
        f"/communities/conversation-room/posts/{post_id}/lock",
        data={"csrf_token": csrf_from(owner_page.text), "locked": "true"},
        follow_redirects=False,
    )
    assert lock.status_code == 303
    assert "discussion is locked" in member.get(post_url).text
    locked_comment = member.post(
        f"/communities/conversation-room/posts/{post_id}/comments",
        data={"csrf_token": csrf_from(member.get("/profile").text), "body": "Too late."},
    )
    assert locked_comment.status_code == 409

    unauthorized_remove = member.post(
        f"/communities/conversation-room/posts/{post_id}/remove",
        data={"csrf_token": csrf_from(member.get("/profile").text)},
    )
    assert unauthorized_remove.status_code == 403

    people = outsider.get("/people")
    owner_id = re.search(r'@owner.*?action="/people/([0-9a-f-]+)/', people.text, re.S).group(1)
    outsider.post(
        f"/people/{owner_id}/block",
        data={"csrf_token": csrf_from(people.text)},
        follow_redirects=False,
    )
    assert outsider.get(post_url).status_code == 404
    assert "A thoughtful topic" not in outsider.get("/communities/conversation-room").text


def test_reports_are_private_to_moderators_and_resolved_with_audit(client_factory):
    owner = client_factory()
    reporter = client_factory()
    other_reporter = client_factory()
    member = client_factory()
    for client, username in [
        (owner, "owner"),
        (reporter, "reporter"),
        (other_reporter, "another"),
        (member, "member"),
    ]:
        assert create_account(client, f"{username}@example.com", username).status_code == 303
    create_community(owner, "report-room")
    post_id = create_post(owner, "report-room", "A reported topic")
    report_url = f"/communities/report-room/reports/post/{post_id}"

    first_report = reporter.post(
        report_url,
        data={"csrf_token": csrf_from(reporter.get("/profile").text), "reason": "Needs moderator review."},
        follow_redirects=False,
    )
    assert first_report.status_code == 303
    duplicate = reporter.post(
        report_url,
        data={"csrf_token": csrf_from(reporter.get("/profile").text), "reason": "Duplicate report."},
    )
    assert "already-reported" in str(duplicate.url)

    second_report = other_reporter.post(
        report_url,
        data={"csrf_token": csrf_from(other_reporter.get("/profile").text), "reason": "Another concern."},
        follow_redirects=False,
    )
    assert second_report.status_code == 303
    assert member.get("/communities/report-room/reports").status_code == 403

    queue_url = "/communities/report-room/reports"
    queue = owner.get(queue_url)
    assert queue.status_code == 200
    assert "Reported by @reporter" in queue.text
    report_ids = list(dict.fromkeys(re.findall(r"/report-queue/([0-9a-f-]+)/resolve", queue.text)))
    assert len(report_ids) == 2

    remove = owner.post(
        f"/communities/report-room/report-queue/{report_ids[0]}/resolve",
        data={"csrf_token": csrf_from(queue.text), "decision": "remove"},
        follow_redirects=False,
    )
    assert remove.status_code == 303, remove.text
    updated_queue = owner.get(queue_url)
    dismiss = owner.post(
        f"/communities/report-room/report-queue/{report_ids[1]}/resolve",
        data={"csrf_token": csrf_from(updated_queue.text), "decision": "dismiss"},
        follow_redirects=False,
    )
    assert dismiss.status_code == 303
    assert "No open reports" in owner.get(queue_url).text
    assert reporter.get(f"/communities/report-room/posts/{post_id}").status_code == 404
    moderated_post = owner.get(f"/communities/report-room/posts/{post_id}")
    assert "This post was removed" in moderated_post.text
    assert "Post Removed" in moderated_post.text