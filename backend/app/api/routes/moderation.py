"""Reporting, the moderator queue, and moderation decisions."""

from datetime import UTC, datetime
from uuid import UUID
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.api.deps import allow_request, authenticated_form_user, authenticated_user, current_session
from app.api.responses import render
from app.core import events
from app.core.database import get_db
from app.core.scoping import account_scope, community_scope, platform_scope
from app.models import Comment, ContentReport, ModerationAppeal, Post, User
from app.services.audit import record_audit
from app.services.authorization import (
    find_personal_post,
    find_post,
    find_visible_community,
    is_blocked_between,
    require_community_manager,
    require_platform_moderator,
)
from app.services.communities import add_moderation_event
from app.services.moderation import appeal_for, platform_removal_report
from app.services.notifications import queue_notification
from app.services.outbox import enqueue_event

router = APIRouter()


@router.post("/communities/{slug}/reports/{target_type}/{target_id}")
def report_content(
    slug: str,
    target_type: str,
    target_id: UUID,
    request: Request,
    reason: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    if target_type == "post":
        target = find_post(database, community, target_id)
        author_id = target.author_id
        unavailable = target.removed_at is not None
    elif target_type == "comment":
        target = database.get(Comment, target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Content not found.")
        post = find_post(database, community, target.post_id)
        author_id = target.author_id
        unavailable = target.removed_at is not None or post.removed_at is not None
    else:
        raise HTTPException(status_code=404, detail="Content not found.")
    if unavailable or (user.id == author_id) or is_blocked_between(database, user.id, author_id):
        raise HTTPException(status_code=404, detail="Content not found.")
    reason = reason.strip()
    if not reason or len(reason) > 1000:
        return RedirectResponse(
            f"/communities/{slug}/posts/{target.post_id if target_type == 'comment' else target.id}?error=invalid-report",
            status_code=303,
        )
    if not allow_request(request, "report-content"):
        raise HTTPException(status_code=429, detail="Too many reports. Try again later.")
    database.add(
        ContentReport(
            community_id=community.id,
            reporter_id=user.id,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
        )
    )
    # The report reason stays in content_reports only: it is redacted when the reporter's
    # account is deleted, so it never belongs in a published event payload.
    enqueue_event(
        database,
        event=events.POST_REPORTED if target_type == "post" else events.COMMENT_REPORTED,
        aggregate_type=target_type,
        aggregate_id=target_id,
        scope=community_scope(community.id),
        payload={"community_id": str(community.id), "reporter_id": str(user.id)},
    )
    try:
        database.commit()
    except IntegrityError:
        database.rollback()
        post_id = target.post_id if target_type == "comment" else target.id
        return RedirectResponse(
            f"/communities/{slug}/posts/{post_id}?error=already-reported", status_code=303
        )
    post_id = target.post_id if target_type == "comment" else target.id
    return RedirectResponse(f"/communities/{slug}/posts/{post_id}", status_code=303)


@router.get("/communities/{slug}/reports", response_class=HTMLResponse)
def report_queue(
    slug: str,
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    community = find_visible_community(database, slug, user)
    if user is None:
        raise HTTPException(status_code=403, detail="Sign in to continue.")
    require_community_manager(database, community, user)
    report_rows = database.scalars(
        select(ContentReport)
        .where(ContentReport.community_id == community.id, ContentReport.status == "open")
        .order_by(ContentReport.created_at)
        .limit(100)
    ).all()
    reports = []
    for report in report_rows:
        target = database.get(Post if report.target_type == "post" else Comment, report.target_id)
        if target is None:
            continue
        if report.target_type == "comment":
            post = database.get(Post, target.post_id)
            author = database.get(User, target.author_id)
        else:
            post = target
            author = database.get(User, target.author_id)
        reporter = database.get(User, report.reporter_id)
        reports.append(
            {
                "report": report,
                "target": target,
                "post": post,
                "author": author,
                "reporter": reporter,
            }
        )
    browser_session = current_session(request, database)
    return render(
        request,
        "communities/reports.html",
        {
            "user": user,
            "community": community,
            "reports": reports,
            "csrf_token": browser_session.csrf_token,
        },
    )


@router.post("/communities/{slug}/report-queue/{report_id}/resolve")
def resolve_report(
    slug: str,
    report_id: UUID,
    request: Request,
    decision: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    require_community_manager(database, community, user)
    report = database.scalar(
        select(ContentReport)
        .where(
            ContentReport.id == report_id,
            ContentReport.community_id == community.id,
            ContentReport.status == "open",
        )
        .with_for_update()
    )
    if report is None:
        raise HTTPException(status_code=404, detail="Open report not found.")
    if decision not in {"remove", "dismiss"}:
        raise HTTPException(status_code=422, detail="Choose remove or dismiss.")
    if decision == "remove":
        target = database.get(Post if report.target_type == "post" else Comment, report.target_id)
        if target is not None and target.removed_at is None:
            target.removed_at = datetime.now(UTC)
            target.removed_by_user_id = user.id
            add_moderation_event(
                database,
                community.id,
                user.id,
                report.target_type,
                target.id,
                f"{report.target_type}_removed",
                report.reason,
                event=events.POST_REMOVED
                if report.target_type == "post"
                else events.COMMENT_REMOVED,
            )
        report.status = "resolved"
    else:
        report.status = "dismissed"
    report.resolved_by_user_id = user.id
    report.resolved_at = datetime.now(UTC)
    add_moderation_event(
        database,
        community.id,
        user.id,
        "report",
        report.id,
        f"report_{report.status}",
        report.reason,
        event=events.REPORT_RESOLVED
        if report.status == "resolved"
        else events.REPORT_DISMISSED,
    )
    reporter = database.get(User, report.reporter_id)
    if reporter is not None:
        outcome = "removed" if decision == "remove" else "dismissed"
        queue_notification(
            database,
            reporter,
            user,
            "moderation",
            f"A moderator reviewed your report: {outcome}.",
        )
    database.commit()
    return RedirectResponse(f"/communities/{slug}/reports", status_code=303)


# The platform queue: the answer to a report about a personal update, which has no community
# owner, no moderator role, and therefore no other place to go. Every route here is gated by
# `require_platform_moderator` before it reads a row, so a non-moderator learns nothing from
# asking.

PLATFORM_DECISIONS = {"remove", "dismiss", "lock", "suspend"}


def _reported_target(database: Session, post: Post, target_type: str, target_id: UUID):
    """The reported row, when it really belongs to this personal update."""
    if target_type == "post":
        if post.id != target_id:
            raise HTTPException(status_code=404, detail="Content not found.")
        return post
    if target_type != "comment":
        raise HTTPException(status_code=404, detail="Content not found.")
    comment = database.get(Comment, target_id)
    if comment is None or comment.post_id != post.id:
        raise HTTPException(status_code=404, detail="Content not found.")
    return comment


@router.post("/posts/{post_id}/reports/{target_type}/{target_id}")
def report_personal_content(
    post_id: UUID,
    target_type: str,
    target_id: UUID,
    request: Request,
    reason: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    """File a report against a personal update or one of its comments.

    `find_personal_post` already answers 404 to a reader the audience keeps out, so a report
    can only ever name content the reporter was allowed to read. Reporting your own post, or
    one written by a person you are blocked from, is the same 404.
    """
    user, _ = authenticated_form_user(request, database, csrf_token)
    post = find_personal_post(database, post_id, user)
    target = _reported_target(database, post, target_type, target_id)
    author_id = target.author_id
    unavailable = target.removed_at is not None or post.removed_at is not None
    if unavailable or user.id == author_id or is_blocked_between(database, user.id, author_id):
        raise HTTPException(status_code=404, detail="Content not found.")
    reason = reason.strip()
    if not reason or len(reason) > 1000:
        return RedirectResponse(f"/posts/{post.id}?error=invalid-report", status_code=303)
    if not allow_request(request, "report-content"):
        raise HTTPException(status_code=429, detail="Too many reports. Try again later.")
    database.add(
        ContentReport(
            community_id=None,
            reporter_id=user.id,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
        )
    )
    enqueue_event(
        database,
        event=events.POST_REPORTED if target_type == "post" else events.COMMENT_REPORTED,
        aggregate_type=target_type,
        aggregate_id=target_id,
        scope=platform_scope(),
        payload={"post_id": str(post.id), "reporter_id": str(user.id)},
    )
    try:
        database.commit()
    except IntegrityError:
        database.rollback()
        return RedirectResponse(f"/posts/{post.id}?error=already-reported", status_code=303)
    return RedirectResponse(f"/posts/{post.id}", status_code=303)


def _platform_report_rows(database: Session) -> list[dict]:
    rows = database.scalars(
        select(ContentReport)
        .where(ContentReport.community_id.is_(None), ContentReport.status == "open")
        .order_by(ContentReport.created_at)
        .limit(100)
    ).all()
    reports = []
    for report in rows:
        target = database.get(
            Post if report.target_type == "post" else Comment, report.target_id
        )
        if target is None:
            # The reported row is gone; the report has nothing left to decide.
            continue
        post = target if report.target_type == "post" else database.get(Post, target.post_id)
        reports.append(
            {
                "report": report,
                "target": target,
                "post": post,
                "author": database.get(User, target.author_id),
                "reporter": database.get(User, report.reporter_id),
            }
        )
    return reports


@router.get("/moderation", response_class=HTMLResponse)
def platform_queue(
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    require_platform_moderator(user)
    browser_session = current_session(request, database)
    return render(
        request,
        "moderation/queue.html",
        {
            "user": user,
            "reports": _platform_report_rows(database),
            "appeals": _appeal_rows(database),
            "csrf_token": browser_session.csrf_token,
        },
    )


@router.post("/moderation/reports/{report_id}/resolve")
def resolve_platform_report(
    report_id: UUID,
    request: Request,
    decision: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    require_platform_moderator(user)
    if decision not in PLATFORM_DECISIONS:
        raise HTTPException(
            status_code=422, detail="Choose remove, dismiss, lock, or suspend."
        )
    report = database.scalar(
        select(ContentReport)
        .where(
            ContentReport.id == report_id,
            ContentReport.community_id.is_(None),
            ContentReport.status == "open",
        )
        .with_for_update()
    )
    if report is None:
        raise HTTPException(status_code=404, detail="Open report not found.")
    target = database.get(
        Post if report.target_type == "post" else Comment, report.target_id
    )
    if target is None:
        raise HTTPException(status_code=404, detail="The reported content is gone.")
    post = target if report.target_type == "post" else database.get(Post, target.post_id)
    author = database.get(User, target.author_id)
    decided_at = datetime.now(UTC)

    if decision == "remove" and target.removed_at is None:
        target.removed_at = decided_at
        target.removed_by_user_id = user.id
        record_audit(
            database,
            action=events.POST_REMOVED
            if report.target_type == "post"
            else events.COMMENT_REMOVED,
            scope=platform_scope(),
            actor_user_id=user.id,
            target_type=report.target_type,
            target_id=target.id,
            after_state={"removed_at": decided_at.isoformat()},
            reason=report.reason,
        )
        enqueue_event(
            database,
            event=events.POST_REMOVED
            if report.target_type == "post"
            else events.COMMENT_REMOVED,
            aggregate_type=report.target_type,
            aggregate_id=target.id,
            scope=platform_scope(),
            payload={"moderator_id": str(user.id), "post_id": str(post.id) if post else None},
        )
    elif decision == "lock" and post is not None and not post.is_locked:
        post.is_locked = True
        record_audit(
            database,
            action=events.POST_LOCKED,
            scope=platform_scope(),
            actor_user_id=user.id,
            target_type="post",
            target_id=post.id,
            before_state={"is_locked": False},
            after_state={"is_locked": True},
            reason=report.reason,
        )
        enqueue_event(
            database,
            event=events.POST_LOCKED,
            aggregate_type="post",
            aggregate_id=post.id,
            scope=platform_scope(),
            payload={"moderator_id": str(user.id)},
        )
    elif decision == "suspend" and author is not None and author.is_active:
        author.is_active = False
        record_audit(
            database,
            action=events.ACCOUNT_SUSPENDED,
            scope=account_scope(author.id),
            actor_user_id=user.id,
            target_type="account",
            target_id=author.id,
            before_state={"is_active": True},
            after_state={"is_active": False},
            reason=report.reason,
        )
        enqueue_event(
            database,
            event=events.ACCOUNT_SUSPENDED,
            aggregate_type="account",
            aggregate_id=author.id,
            scope=account_scope(author.id),
            payload={"moderator_id": str(user.id)},
        )

    report.status = "dismissed" if decision == "dismiss" else "resolved"
    report.resolved_by_user_id = user.id
    report.resolved_at = decided_at
    record_audit(
        database,
        action=events.REPORT_RESOLVED
        if report.status == "resolved"
        else events.REPORT_DISMISSED,
        scope=platform_scope(),
        actor_user_id=user.id,
        target_type="report",
        target_id=report.id,
        before_state={"status": "open"},
        after_state={"status": report.status, "decision": decision},
        reason=report.reason,
    )
    enqueue_event(
        database,
        event=events.REPORT_RESOLVED
        if report.status == "resolved"
        else events.REPORT_DISMISSED,
        aggregate_type="report",
        aggregate_id=report.id,
        scope=platform_scope(),
        payload={"moderator_id": str(user.id), "decision": decision},
    )

    # A suspended account cannot sign in to read a notification, so only the decisions whose
    # author can still read are announced to them.
    if author is not None and decision in {"remove", "lock"} and post is not None:
        queue_notification(
            database,
            author,
            user,
            "moderation",
            "A moderator reviewed one of your updates."
            if decision == "remove"
            else "A moderator locked comments on one of your updates.",
            f"/posts/{post.id}",
        )
    reporter = database.get(User, report.reporter_id)
    if reporter is not None:
        outcome = {
            "remove": "the post was removed",
            "lock": "comments were locked",
            "suspend": "the account was suspended",
            "dismiss": "no action was taken",
        }[decision]
        queue_notification(
            database,
            reporter,
            user,
            "moderation",
            f"A moderator reviewed your report: {outcome}.",
        )
    database.commit()
    return RedirectResponse("/moderation", status_code=303)


# Appeals: the author's one chance to question a removal the platform queue decided. The queue
# cannot overturn itself without a record, and the author cannot file the same appeal twice.


def _appeal_rows(database: Session) -> list[dict]:
    rows = database.scalars(
        select(ModerationAppeal)
        .where(ModerationAppeal.status == "pending")
        .order_by(ModerationAppeal.created_at)
        .limit(100)
    ).all()
    appeals = []
    for appeal in rows:
        target = database.get(
            Post if appeal.target_type == "post" else Comment, appeal.target_id
        )
        # `post` is the thread the appeal hangs off, which is the thing a moderator wants to
        # open; `target` is what was actually removed.
        post = target if appeal.target_type == "post" else database.get(Post, getattr(target, "post_id", None))
        report = database.get(ContentReport, appeal.report_id) if appeal.report_id else None
        appeals.append(
            {
                "appeal": appeal,
                "target": target,
                "post": post,
                "author": database.get(User, appeal.author_id),
                "report": report,
            }
        )
    return appeals


@router.post("/posts/{post_id}/appeals/{target_type}/{target_id}")
def file_moderation_appeal(
    post_id: UUID,
    target_type: str,
    target_id: UUID,
    request: Request,
    reason: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    """The author asks for a removal to be reviewed.

    Every refusal is the non-revealing 404 a missing post returns: a reader who cannot see the
    post, an appeal against someone else's content, content nobody removed, and a removal this
    queue did not decide — a community moderator's, or an author's own withdrawal.

    A comment is appealable by the person who wrote it, not by the post's author: the moderator
    removed that person's words, so that person is who the decision was about.
    """
    user, _ = authenticated_form_user(request, database, csrf_token)
    post = find_personal_post(database, post_id, user)
    if target_type == "post":
        if target_id != post.id or post.author_id != user.id or post.removed_at is None:
            raise HTTPException(status_code=404, detail="Content not found.")
        removal = platform_removal_report(database, "post", post.id)
    elif target_type == "comment":
        comment = database.get(Comment, target_id)
        if (
            comment is None
            or comment.post_id != post.id
            or comment.author_id != user.id
            or comment.removed_at is None
        ):
            raise HTTPException(status_code=404, detail="Content not found.")
        removal = platform_removal_report(database, "comment", comment.id)
    else:
        raise HTTPException(status_code=404, detail="Content not found.")
    if removal is None:
        raise HTTPException(status_code=404, detail="Content not found.")
    reason = reason.strip()
    if not reason or len(reason) > 2000:
        return RedirectResponse(f"/posts/{post.id}?error=invalid-appeal", status_code=303)
    if not allow_request(request, "moderation-appeal"):
        raise HTTPException(status_code=429, detail="Too many appeals. Try again later.")
    if appeal_for(database, target_type, target_id, user.id) is not None:
        return RedirectResponse(f"/posts/{post.id}?error=already-appealed", status_code=303)
    appeal = ModerationAppeal(
        author_id=user.id,
        report_id=removal.id,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
    )
    try:
        database.add(appeal)
        database.flush()
        enqueue_event(
            database,
            event=events.MODERATION_APPEAL_FILED,
            aggregate_type="appeal",
            aggregate_id=appeal.id,
            scope=platform_scope(),
            payload={"post_id": str(post.id), "author_id": str(user.id)},
        )
        database.commit()
    except IntegrityError:
        # The pre-check above loses a race with a second tab; the unique constraint is what
        # actually decides, and the answer is the same.
        database.rollback()
        return RedirectResponse(f"/posts/{post.id}?error=already-appealed", status_code=303)
    return RedirectResponse(f"/posts/{post.id}", status_code=303)


@router.post("/moderation/appeals/{appeal_id}/resolve")
def resolve_moderation_appeal(
    appeal_id: UUID,
    request: Request,
    decision: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    require_platform_moderator(user)
    if decision not in {"grant", "deny"}:
        raise HTTPException(status_code=422, detail="Choose grant or deny.")
    appeal = database.scalar(
        select(ModerationAppeal)
        .where(ModerationAppeal.id == appeal_id, ModerationAppeal.status == "pending")
        .with_for_update()
    )
    if appeal is None:
        raise HTTPException(status_code=404, detail="Open appeal not found.")
    target = database.get(
        Post if appeal.target_type == "post" else Comment, appeal.target_id
    )
    decided_at = datetime.now(UTC)
    restoring = decision == "grant"
    if restoring and target is not None and target.removed_at is not None:
        target.removed_at = None
        target.removed_by_user_id = None
    appeal.status = "granted" if restoring else "denied"
    appeal.resolved_by_user_id = user.id
    appeal.resolved_at = decided_at
    action = (
        events.MODERATION_APPEAL_GRANTED if restoring else events.MODERATION_APPEAL_DENIED
    )
    record_audit(
        database,
        action=action,
        scope=platform_scope(),
        actor_user_id=user.id,
        target_type="appeal",
        target_id=appeal.id,
        before_state={"status": "pending"},
        after_state={"status": appeal.status, "content_restored": restoring},
        reason=appeal.reason,
    )
    enqueue_event(
        database,
        event=action,
        aggregate_type="appeal",
        aggregate_id=appeal.id,
        scope=platform_scope(),
        payload={
            "moderator_id": str(user.id),
            "target_type": appeal.target_type,
            "target_id": str(appeal.target_id),
        },
    )
    author = database.get(User, appeal.author_id)
    if author is not None:
        # A comment's appeal lands on the post that holds it, so the notice is useful either way.
        post_id = (
            target.id
            if appeal.target_type == "post" and target is not None
            else getattr(target, "post_id", None)
        )
        if appeal.target_type == "comment":
            message = (
                "A moderator granted your appeal: your comment is visible again."
                if restoring
                else "A moderator reviewed your appeal and the comment removal stands."
            )
        else:
            message = (
                "A moderator granted your appeal: the update is visible again."
                if restoring
                else "A moderator reviewed your appeal and the removal stands."
            )
        queue_notification(
            database,
            author,
            user,
            "moderation",
            message,
            f"/posts/{post_id}" if post_id is not None else None,
        )
    database.commit()
    return RedirectResponse("/moderation", status_code=303)
