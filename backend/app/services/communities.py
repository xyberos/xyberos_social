"""Community lifecycle and moderation bookkeeping."""

from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import (
    Comment,
    Community,
    CommunityAuditEvent,
    CommunityMembership,
    ContentReport,
    ModerationEvent,
    Post,
)


def delete_unowned_community(database: Session, community: Community) -> None:
    post_ids = select(Post.id).where(Post.community_id == community.id)
    database.query(ContentReport).filter(ContentReport.community_id == community.id).delete(
        synchronize_session=False
    )
    database.query(ModerationEvent).filter(ModerationEvent.community_id == community.id).delete(
        synchronize_session=False
    )
    database.query(CommunityAuditEvent).filter(
        CommunityAuditEvent.community_id == community.id
    ).delete(synchronize_session=False)
    database.query(Comment).filter(Comment.post_id.in_(post_ids)).delete(synchronize_session=False)
    database.query(Post).filter(Post.community_id == community.id).delete(synchronize_session=False)
    database.query(CommunityMembership).filter(
        CommunityMembership.community_id == community.id
    ).delete(synchronize_session=False)
    database.delete(community)


def add_moderation_event(
    database: Session,
    community_id: UUID,
    moderator_id: UUID,
    target_type: str,
    target_id: UUID,
    action: str,
    details: str = "",
) -> None:
    database.add(
        ModerationEvent(
            community_id=community_id,
            moderator_id=moderator_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            details=details,
        )
    )
