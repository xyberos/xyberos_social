"""Visibility and role rules shared by communities, content, chat, and moderation."""

from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import false, or_, select, true
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


def is_following(database: Session, follower_id, author_id) -> bool:
    return (
        database.scalar(
            select(UserRelationship.source_user_id).where(
                UserRelationship.source_user_id == follower_id,
                UserRelationship.target_user_id == author_id,
                UserRelationship.kind == "follow",
            )
        )
        is not None
    )


def can_read_personal_post(database: Session, viewer: User | None, post: Post) -> bool:
    """The per-row audience rule: the author, and then whoever the audience names.

    `public` is the only value a reader without an account can satisfy, and it is settled
    before the viewer is required precisely because the open web has no identity to consult.
    A block is absolute wherever an identity exists, so it still hides a public update from the
    person the author blocked; an anonymous reader is nobody's blocked pair.
    `visible_personal_posts_condition` states the same rule as one SQL predicate for a page
    that lists a whole profile.
    """
    if viewer is not None and viewer.id == post.author_id:
        return True
    if viewer is not None and is_blocked_between(database, viewer.id, post.author_id):
        return False
    if post.audience == "public":
        return True
    if viewer is None:
        return False
    if post.audience == "everyone":
        return True
    return post.audience == "followers" and is_following(database, viewer.id, post.author_id)


def visible_personal_posts_condition(database: Session, viewer: User | None, author_id: UUID):
    """`can_read_personal_post` as a predicate, asked once for a whole profile page."""
    if viewer is not None and viewer.id == author_id:
        return true()
    if viewer is not None and is_blocked_between(database, viewer.id, author_id):
        return false()
    if viewer is None:
        # A signed-out reader sees exactly what the author opened to the web.
        return Post.audience == "public"
    if is_following(database, viewer.id, author_id):
        return Post.audience.in_(("followers", "everyone", "public"))
    return Post.audience.in_(("everyone", "public"))


def find_personal_post(database: Session, post_id: UUID, user: User | None) -> Post:
    """A personal update, if the viewer's audience rule allows reading it.

    Outsiders get the same 404 a missing post returns, so the page never confirms that a post
    they may not read exists.
    """
    post = database.get(Post, post_id)
    if post is None or post.community_id is not None:
        raise HTTPException(status_code=404, detail="Post not found.")
    # A deleted account keeps its rows but not its audience. Deletion deactivates the account
    # and erases who it was, so nothing it published may stay readable afterwards — least of
    # all on the open web.
    author = database.get(User, post.author_id)
    if author is None or not author.is_active:
        raise HTTPException(status_code=404, detail="Post not found.")
    if not can_read_personal_post(database, user, post):
        raise HTTPException(status_code=404, detail="Post not found.")
    # A removed update stays readable to its author, who is the only one who can restore or
    # clear it, and disappears for everyone else.
    if post.removed_at is not None and (user is None or user.id != post.author_id):
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


def require_platform_moderator(user: User) -> None:
    """The platform queue is a role, and the role is granted outside the application.

    Personal updates have no community owner to appeal to, so the queue that answers a report
    about one is platform-wide. Nothing here makes the caller one; an operator sets the flag.
    """
    if not user.is_moderator:
        raise HTTPException(
            status_code=403, detail="You do not have permission to moderate."
        )
