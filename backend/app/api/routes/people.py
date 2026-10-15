"""Directory of accounts and follow/block relationships."""

from uuid import UUID
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from app.api.deps import authenticated_form_user, authenticated_user, current_session
from app.api.responses import redirect_people, render
from app.core.database import get_db
from app.models import User, UserRelationship
from app.services.authorization import is_blocked_between

router = APIRouter()


@router.get("/people", response_class=HTMLResponse)
def people_page(
    request: Request,
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
    users = database.scalars(
        select(User)
        .where(User.id != user.id, User.is_active.is_(True))
        .order_by(User.username)
        .limit(100)
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
    browser_session = current_session(request, database)
    return render(
        request,
        "people.html",
        {
            "user": user,
            "people": people,
            "message": request.query_params.get("message"),
            "csrf_token": browser_session.csrf_token if browser_session else None,
        },
    )


@router.post("/people/{target_id}/follow")
def follow_user(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    target = database.get(User, target_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot follow yourself.")
    if is_blocked_between(database, user.id, target.id):
        return redirect_people("A block prevents this follow.")
    edge = database.get(UserRelationship, (user.id, target.id, "follow"))
    if edge is None:
        database.add(
            UserRelationship(source_user_id=user.id, target_user_id=target.id, kind="follow")
        )
        database.commit()
    return redirect_people()


@router.post("/people/{target_id}/unfollow")
def unfollow_user(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    edge = database.get(UserRelationship, (user.id, target_id, "follow"))
    if edge is not None:
        database.delete(edge)
        database.commit()
    return redirect_people()


@router.post("/people/{target_id}/block")
def block_user(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
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
    database.commit()
    return redirect_people()


@router.post("/people/{target_id}/unblock")
def unblock_user(
    target_id: UUID,
    request: Request,
    csrf_token: str = Form(),
    database: Session = Depends(get_db),
):
    user, _ = authenticated_form_user(request, database, csrf_token)
    edge = database.get(UserRelationship, (user.id, target_id, "block"))
    if edge is not None:
        database.delete(edge)
        database.commit()
    return redirect_people()
