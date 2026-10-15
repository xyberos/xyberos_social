"""Visibility and role rules shared by communities, content, chat, and moderation."""

from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from app.models import Community, CommunityMembership, Post, User, UserRelationship


def community_role(database: Session, community_id, user_id) -> str | None:
    membership = database.get(CommunityMembership, (community_id, user_id))
    return membership.role if membership else None


def find_visible_community(database: Session, slug: str, user: User | None) -> Community:
    community = database.scalar(select(Community).where(Community.slug == slug))
    if community is None:
        raise HTTPException(status_code=404, detail="Community not found.")
    if community.visibility == "private" and (
        user is None or community_role(database, community.id, user.id) is None
    ):
        raise HTTPException(status_code=404, detail="Community not found.")
    return community


def is_blocked_between(database: Session, first_user_id, second_user_id) -> bool:
    return (
        database.scalar(
            select(UserRelationship.source_user_id).where(
                UserRelationship.kind == "block",
                or_(
                    (UserRelationship.source_user_id == first_user_id)
                    & (UserRelationship.target_user_id == second_user_id),
                    (UserRelationship.source_user_id == second_user_id)
                    & (UserRelationship.target_user_id == first_user_id),
                ),
            )
        )
        is not None
    )


def blocked_user_ids(database: Session, user_id) -> set[UUID]:
    relationships = database.scalars(
        select(UserRelationship).where(
            UserRelationship.kind == "block",
            or_(
                UserRelationship.source_user_id == user_id,
                UserRelationship.target_user_id == user_id,
            ),
        )
    ).all()
    return {
        relation.target_user_id if relation.source_user_id == user_id else relation.source_user_id
        for relation in relationships
    }


def require_community_member(database: Session, community: Community, user: User) -> str:
    role = community_role(database, community.id, user.id)
    if role is None:
        raise HTTPException(status_code=403, detail="Join the community to participate.")
    return role


def find_post(database: Session, community: Community, post_id: UUID) -> Post:
    post = database.get(Post, post_id)
    if post is None or post.community_id != community.id:
        raise HTTPException(status_code=404, detail="Post not found.")
    return post


def require_community_manager(
    database: Session, community: Community, user: User, owner_only: bool = False
) -> str:
    role = community_role(database, community.id, user.id)
    allowed = {"owner"} if owner_only else {"owner", "moderator"}
    if role not in allowed:
        raise HTTPException(
            status_code=403, detail="You do not have permission to manage this community."
        )
    return role
