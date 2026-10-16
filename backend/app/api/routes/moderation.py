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
from app.core.scoping import community_scope
from app.models import Comment, ContentReport, Post, User
from app.services.authorization import (
    find_post,
    find_visible_community,
    is_blocked_between,
    require_community_manager,
)
from app.services.communities import add_moderation_event
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
