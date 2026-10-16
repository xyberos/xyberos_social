"""Communities, visibility, membership, and roles."""

import re
from uuid import UUID
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.api.deps import authenticated_form_user, authenticated_user, current_session, form_context
from app.api.responses import render
from app.core import events
from app.core.database import get_db
from app.models import Community, CommunityAuditEvent, CommunityMembership, Post, User
from app.services.authorization import (
    blocked_user_ids,
    community_role,
    find_visible_community,
    is_blocked_between,
    require_community_manager,
)
from app.services.communities import record_community_audit

router = APIRouter()


@router.get("/communities", response_class=HTMLResponse)
def communities_page(
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    visible_condition = Community.visibility == "public"
    if user is not None:
        visible_condition = or_(
            visible_condition,
            Community.id.in_(
                select(CommunityMembership.community_id).where(
                    CommunityMembership.user_id == user.id
                )
            ),
        )
    communities = database.scalars(
        select(Community).where(visible_condition).order_by(Community.name).limit(100)
    ).all()
    roles = (
        {community.id: community_role(database, community.id, user.id) for community in communities}
        if user
        else {}
    )
    browser_session = current_session(request, database) if user else None
    return render(
        request,
        "communities/list.html",
        {
            "user": user,
            "communities": communities,
            "roles": roles,
            "csrf_token": browser_session.csrf_token if browser_session else None,
        },
    )


@router.get("/communities/new", response_class=HTMLResponse)
def new_community_page(
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    browser_session = current_session(request, database)
    return render(request, "communities/new.html", form_context(browser_session, user=user))


@router.post("/communities")
def create_community(
    request: Request,
    name: str = Form(),
    slug: str = Form(),
    description: str = Form(""),
    visibility: str = Form("public"),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, browser_session = authenticated_form_user(request, database, csrf_token)
    name = name.strip()
    slug = slug.strip().lower()
    description = description.strip()
    error = None
    if not name or len(name) > 80:
        error = "Community name must be 1–80 characters."
    elif not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 48:
        error = "Use a 1–48 character slug with lowercase letters, numbers, and single hyphens."
    elif len(description) > 1000:
        error = "Description must be at most 1000 characters."
    elif visibility not in {"public", "private"}:
        error = "Choose public or private visibility."
    elif database.scalar(select(Community.id).where(Community.slug == slug)):
        error = "That community address is already in use."
    if error:
        return render(
            request,
            "communities/new.html",
            form_context(
                browser_session,
                user=user,
                error=error,
                values={
                    "name": name,
                    "slug": slug,
                    "description": description,
                    "visibility": visibility,
                },
            ),
            status_code=422,
        )

    community = Community(
        name=name,
        slug=slug,
        description=description,
        visibility=visibility,
        created_by=user.id,
    )
    database.add(community)
    database.flush()
    database.add(CommunityMembership(community_id=community.id, user_id=user.id, role="owner"))
    record_community_audit(
        database,
        community,
        event=events.COMMUNITY_CREATED,
        action="community_created",
        actor_user_id=user.id,
        details=f"Created {visibility} community {slug}",
        after_state={"name": name, "slug": slug, "visibility": visibility},
    )
    try:
        database.commit()
    except IntegrityError:
        database.rollback()
        return render(
            request,
            "communities/new.html",
            form_context(
                browser_session, user=user, error="That community address is already in use."
            ),
            status_code=422,
        )
    return RedirectResponse(f"/communities/{community.slug}", status_code=303)


@router.get("/communities/{slug}", response_class=HTMLResponse)
def community_detail(
    slug: str,
    request: Request,
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    community = find_visible_community(database, slug, user)
    role = community_role(database, community.id, user.id) if user else None
    members = database.execute(
        select(CommunityMembership, User)
        .join(User, User.id == CommunityMembership.user_id)
        .where(CommunityMembership.community_id == community.id)
        .order_by(CommunityMembership.joined_at)
    ).all()
    events = []
    hidden_author_ids = blocked_user_ids(database, user.id) if user else set()
    post_rows = database.execute(
        select(Post, User)
        .join(User, User.id == Post.author_id)
        .where(Post.community_id == community.id, Post.removed_at.is_(None))
        .order_by(Post.created_at.desc())
        .limit(50)
    ).all()
    posts = [(post, author) for post, author in post_rows if author.id not in hidden_author_ids]
    if role in {"owner", "moderator"}:
        events = database.scalars(
            select(CommunityAuditEvent)
            .where(CommunityAuditEvent.community_id == community.id)
            .order_by(CommunityAuditEvent.created_at.desc())
            .limit(50)
        ).all()
    browser_session = current_session(request, database) if user else None
    return render(
        request,
        "communities/detail.html",
        {
            "user": user,
            "community": community,
            "role": role,
            "members": members,
            "posts": posts,
            "events": events,
            "csrf_token": browser_session.csrf_token if browser_session else None,
        },
    )


@router.post("/communities/{slug}/join")
def join_community(
    slug: str,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = database.scalar(select(Community).where(Community.slug == slug))
    if community is None:
        raise HTTPException(status_code=404, detail="Community not found.")
    if community.visibility != "public":
        raise HTTPException(status_code=404, detail="Community not found.")
    if community_role(database, community.id, user.id) is None:
        database.add(CommunityMembership(community_id=community.id, user_id=user.id, role="member"))
        database.commit()
    return RedirectResponse(f"/communities/{slug}", status_code=303)


@router.post("/communities/{slug}/leave")
def leave_community(
    slug: str,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    membership = database.get(CommunityMembership, (community.id, user.id))
    if membership is not None:
        if membership.role == "owner":
            raise HTTPException(status_code=409, detail="The owner cannot leave the community.")
        database.delete(membership)
        database.commit()
    return RedirectResponse("/communities", status_code=303)


@router.post("/communities/{slug}/members")
def invite_community_member(
    slug: str,
    request: Request,
    username: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    require_community_manager(database, community, user)
    target = database.scalar(select(User).where(User.username == username.strip().lower()))
    if target is None or not target.is_active:
        return RedirectResponse(f"/communities/{slug}?error=member-not-found", status_code=303)
    if is_blocked_between(database, user.id, target.id):
        return RedirectResponse(f"/communities/{slug}?error=blocked", status_code=303)
    if community_role(database, community.id, target.id) is None:
        database.add(
            CommunityMembership(community_id=community.id, user_id=target.id, role="member")
        )
        record_community_audit(
            database,
            community,
            event=events.COMMUNITY_MEMBER_INVITED,
            action="member_invited",
            actor_user_id=user.id,
            target_user_id=target.id,
            details=f"Added @{target.username} as a member",
            after_state={"role": "member"},
        )
        database.commit()
    return RedirectResponse(f"/communities/{slug}", status_code=303)


@router.post("/communities/{slug}/members/{target_id}/role")
def update_community_role(
    slug: str,
    target_id: UUID,
    request: Request,
    role: str = Form(),
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    require_community_manager(database, community, user, owner_only=True)
    if role not in {"member", "moderator"}:
        raise HTTPException(status_code=422, detail="Role must be member or moderator.")
    membership = database.get(CommunityMembership, (community.id, target_id))
    if membership is None or membership.role == "owner":
        raise HTTPException(status_code=404, detail="Member not found.")
    previous_role = membership.role
    if previous_role != role:
        membership.role = role
        record_community_audit(
            database,
            community,
            event=events.COMMUNITY_ROLE_CHANGED,
            action="role_changed",
            actor_user_id=user.id,
            target_user_id=membership.user_id,
            details=f"Changed role from {previous_role} to {role}",
            before_state={"role": previous_role},
            after_state={"role": role},
        )
        database.commit()
    return RedirectResponse(f"/communities/{slug}", status_code=303)


@router.post("/communities/{slug}/members/{target_id}/remove")
def remove_community_member(
    slug: str,
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    community = find_visible_community(database, slug, user)
    actor_role = require_community_manager(database, community, user)
    membership = database.get(CommunityMembership, (community.id, target_id))
    if membership is None or membership.role == "owner":
        raise HTTPException(status_code=404, detail="Member not found.")
    if actor_role != "owner" and membership.role != "member":
        raise HTTPException(status_code=403, detail="Moderators may remove members only.")
    record_community_audit(
        database,
        community,
        event=events.COMMUNITY_MEMBER_REMOVED,
        action="member_removed",
        actor_user_id=user.id,
        target_user_id=membership.user_id,
        details=f"Removed @{membership.user.username} from the community",
        before_state={"role": membership.role},
    )
    database.delete(membership)
    database.commit()
    return RedirectResponse(f"/communities/{slug}", status_code=303)
