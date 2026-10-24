"""Directory of accounts and follow/block relationships."""

from uuid import UUID
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import not_, or_, select
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.api.deps import authenticated_form_user, authenticated_user, current_session
from app.api.responses import redirect_people, render
from app.core.database import get_db
from app.core import events
from app.core.scoping import platform_scope
from app.models import Community, Post, User, UserRelationship
from app.services import calls as calls_service
from app.services import attachments as attachment_service
from app.services.outbox import enqueue_event
from app.services.authorization import (
    is_blocked_between,
    is_following,
    visible_personal_posts_condition,
)

router = APIRouter()


@router.get("/people", response_class=HTMLResponse)
def people_page(
    request: Request,
    following_page: int = Query(default=1, ge=1),
    others_page: int = Query(default=1, ge=1),
    search: str = Query(default="", max_length=80),
    database: Session = Depends(get_db),
):
    user = authenticated_user(request, database)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    relationships = database.scalars(
        select(UserRelationship).where(
            or_(
                UserRelationship.source_user_id == user.id,
                UserRelationship.target_user_id == user.id,
            )
        )
    ).all()
    following = {
        item.target_user_id
        for item in relationships
        if item.kind == "follow" and item.source_user_id == user.id
    }
    following_by = {
        item.source_user_id
        for item in relationships
        if item.kind == "follow" and item.target_user_id == user.id
    }
    blocked = {
        item.target_user_id
        for item in relationships
        if item.kind == "block" and item.source_user_id == user.id
    }
    blocked_by = {
        item.source_user_id
        for item in relationships
        if item.kind == "block" and item.target_user_id == user.id
    }
    search = search.strip()
    user_conditions = [User.id != user.id, User.is_active.is_(True)]
    if search:
        pattern = f"%{search}%"
        user_conditions.append(
            or_(func.lower(User.username).like(func.lower(pattern)),
                func.lower(User.display_name).like(func.lower(pattern)))
        )
    users = database.scalars(
        select(User)
        .where(*user_conditions)
        .order_by(User.created_at.desc(), User.id.desc())
    ).all()
    people = [
        {
            "user": person,
            "following": person.id in following,
            "follows_you": person.id in following_by,
            "blocked": person.id in blocked,
            "blocked_you": person.id in blocked_by,
        }
        for person in users
    ]
    followed_people = sorted(
        (person for person in people if person["following"]),
        key=lambda person: (person["user"].created_at, person["user"].id),
        reverse=True,
    )
    other_people = sorted(
        (person for person in people if not person["following"]),
        key=lambda person: (person["user"].created_at, person["user"].id),
        reverse=True,
    )
    page_size = 10
    followed_count = len(followed_people)
    others_count = len(other_people)
    followed_people = followed_people[(following_page - 1) * page_size : following_page * page_size]
    other_people = other_people[(others_page - 1) * page_size : others_page * page_size]
    browser_session = current_session(request, database)
    return render(
        request,
        "people/list.html",
        {
            "user": user,
            "people": people,
            "followed_people": followed_people,
            "other_people": other_people,
            "following_page": following_page,
            "others_page": others_page,
            "following_pages": max(1, (followed_count + page_size - 1) // page_size),
            "others_pages": max(1, (others_count + page_size - 1) // page_size),
            "followed_count": followed_count,
            "others_count": others_count,
            "search": search,
            "message": request.query_params.get("message"),
            "csrf_token": browser_session.csrf_token if browser_session else None,
        },
    )


@router.get("/people/{target_id}", response_class=HTMLResponse)
def person_profile(
    target_id: UUID,
    request: Request,
    database: Session = Depends(get_db),
):
    viewer = authenticated_user(request, database)
    if viewer is None:
        return RedirectResponse("/login", status_code=303)
    person = database.get(User, target_id)
    if person is None or not person.is_active:
        raise HTTPException(status_code=404, detail="User not found.")
    if person.id == viewer.id:
        return RedirectResponse("/profile", status_code=303)
    # A profile always exists; which of its updates it shows depends on each post's audience.
    visible = visible_personal_posts_condition(database, viewer, person.id)
    posts = database.scalars(
        select(Post)
        .outerjoin(Community, Community.id == Post.community_id)
        .where(
            Post.author_id == person.id,
            Post.removed_at.is_(None),
            or_(Post.community_id.is_(None), Community.visibility == "public"),
            visible,
        )
        .order_by(Post.created_at.desc(), Post.id.desc())
        .limit(20)
    ).all()
    media = attachment_service.post_media_payloads(database, [post.id for post in posts])
    # The hint explains an empty list only when updates exist that this viewer may not read.
    hidden_updates = (
        not posts
        and database.scalar(
            select(Post.id)
            .where(
                Post.author_id == person.id,
                Post.community_id.is_(None),
                Post.removed_at.is_(None),
                not_(visible),
            )
            .limit(1)
        )
        is not None
    )
    browser_session = current_session(request, database)
    return render(
        request,
        "people/person.html",
        {
            "user": viewer,
            "person": person,
            "communities": {
                post.id: database.get(Community, post.community_id)
                for post in posts
                if post.community_id is not None
            },
            "following": is_following(database, viewer.id, person.id),
            "blocked": database.get(UserRelationship, (viewer.id, person.id, "block"))
            is not None,
            "blocked_you": database.get(UserRelationship, (person.id, viewer.id, "block"))
            is not None,
            "hidden_updates": hidden_updates,
            "posts": posts,
            "media": media,
            "message": request.query_params.get("message"),
            "csrf_token": browser_session.csrf_token if browser_session else None,
        },
    )


@router.post("/people/{target_id}/follow")
def follow_user(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    next_url: str | None = Form(default=None, alias="next"),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    target = database.get(User, target_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot follow yourself.")
    if is_blocked_between(database, user.id, target.id):
        return redirect_people("A block prevents this follow.", next_url=next_url)
    edge = database.get(UserRelationship, (user.id, target.id, "follow"))
    if edge is None:
        database.add(
            UserRelationship(source_user_id=user.id, target_user_id=target.id, kind="follow")
        )
        enqueue_event(
            database,
            event=events.SOCIAL_RELATIONSHIP_CHANGED,
            aggregate_type="user",
            aggregate_id=target.id,
            scope=platform_scope(),
            payload={"actor_id": str(user.id), "target_id": str(target.id), "kind": "follow"},
        )
        database.commit()
    return redirect_people(next_url=next_url)


@router.post("/people/{target_id}/unfollow")
def unfollow_user(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    next_url: str | None = Form(default=None, alias="next"),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    edge = database.get(UserRelationship, (user.id, target_id, "follow"))
    if edge is not None:
        database.delete(edge)
        enqueue_event(
            database,
            event=events.SOCIAL_RELATIONSHIP_CHANGED,
            aggregate_type="user",
            aggregate_id=target_id,
            scope=platform_scope(),
            payload={"actor_id": str(user.id), "target_id": str(target_id), "kind": "unfollow"},
        )
        database.commit()
    return redirect_people(next_url=next_url)


@router.post("/people/{target_id}/block")
async def block_user(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    next_url: str | None = Form(default=None, alias="next"),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    target = database.get(User, target_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot block yourself.")
    if database.get(UserRelationship, (user.id, target.id, "block")) is None:
        database.add(
            UserRelationship(source_user_id=user.id, target_user_id=target.id, kind="block")
        )
    follows = database.scalars(
        select(UserRelationship).where(
            UserRelationship.kind == "follow",
            or_(
                (UserRelationship.source_user_id == user.id)
                & (UserRelationship.target_user_id == target.id),
                (UserRelationship.source_user_id == target.id)
                & (UserRelationship.target_user_id == user.id),
            ),
        )
    ).all()
    for follow in follows:
        database.delete(follow)
    enqueue_event(
        database,
        event=events.SOCIAL_RELATIONSHIP_CHANGED,
        aggregate_type="user",
        aggregate_id=target.id,
        scope=platform_scope(),
        payload={"actor_id": str(user.id), "target_id": str(target.id), "kind": "block"},
    )
    # A block removes both accounts from any call they share, before it is committed.
    call_changes = calls_service.remove_blocked_pair_from_calls(database, user.id, target.id)
    database.commit()
    if call_changes:
        await calls_service.publish_calls(database, call_changes)
    return redirect_people(next_url=next_url)


@router.post("/people/{target_id}/unblock")
def unblock_user(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    next_url: str | None = Form(default=None, alias="next"),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    edge = database.get(UserRelationship, (user.id, target_id, "block"))
    if edge is not None:
        database.delete(edge)
        enqueue_event(
            database,
            event=events.SOCIAL_RELATIONSHIP_CHANGED,
            aggregate_type="user",
            aggregate_id=target_id,
            scope=platform_scope(),
            payload={"actor_id": str(user.id), "target_id": str(target_id), "kind": "unblock"},
        )
        database.commit()
    return redirect_people(next_url=next_url)
