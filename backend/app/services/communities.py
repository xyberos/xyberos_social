"""Community lifecycle and moderation bookkeeping."""

from typing import Any
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core import events
from app.core.scoping import community_scope, platform_scope
from app.models import (
    Attachment,
    Comment,
    Community,
    CommunityAuditEvent,
    CommunityMembership,
    ContentReport,
    ModerationEvent,
    OutboxEvent,
    PlatformAuditEvent,
    Post,
)
from app.services.attachments import delete_attachments
from app.services.audit import record_audit
from app.services.outbox import enqueue_event


def delete_unowned_community(database: Session, community: Community) -> None:
    """Remove a community and every record scoped to it.

    The Phase 6 retention decision deletes a community's audit history with it, so the
    platform log and outbox rows for the scope are removed too. Those rows carry no
    foreign key on `scope_id`, which is what lets an audit row outlive a deleted target
    while still being removable when the tenant itself goes away.
    """
    scope = community_scope(community.id)
    post_ids = select(Post.id).where(Post.community_id == community.id)
    database.query(OutboxEvent).filter(
        OutboxEvent.scope_type == scope.kind, OutboxEvent.scope_id == scope.id
    ).delete(synchronize_session=False)
    database.query(PlatformAuditEvent).filter(
        PlatformAuditEvent.scope_type == scope.kind, PlatformAuditEvent.scope_id == scope.id
    ).delete(synchronize_session=False)
    # The removal itself is recorded at platform scope, which is the one scope that
    # survives the tenant, so a deleted community is still accounted for.
    record_audit(
        database,
        action=events.COMMUNITY_DELETED,
        scope=platform_scope(),
        target_type="community",
        target_id=community.id,
        reason="No active member remained after account deletion.",
        after_state={"slug": community.slug, "name": community.name},
    )
    enqueue_event(
        database,
        event=events.COMMUNITY_DELETED,
        aggregate_type="community",
        aggregate_id=community.id,
        scope=platform_scope(),
        payload={"slug": community.slug},
    )
    database.query(ContentReport).filter(ContentReport.community_id == community.id).delete(
        synchronize_session=False
    )
    database.query(ModerationEvent).filter(ModerationEvent.community_id == community.id).delete(
        synchronize_session=False
    )
    database.query(CommunityAuditEvent).filter(
        CommunityAuditEvent.community_id == community.id
    ).delete(synchronize_session=False)
    # Post media lives outside PostgreSQL, so a cascading delete cannot reach it. The objects
    # (original and preview) are removed explicitly, and the rows are flushed before the posts
    # are dropped so the database cascade does not try to delete them a second time.
    post_media = list(
        database.scalars(select(Attachment).where(Attachment.post_id.in_(post_ids)))
    )
    delete_attachments(database, post_media)
    database.flush()
    database.query(Comment).filter(Comment.post_id.in_(post_ids)).delete(synchronize_session=False)
    database.query(Post).filter(Post.community_id == community.id).delete(synchronize_session=False)
    database.query(CommunityMembership).filter(
        CommunityMembership.community_id == community.id
    ).delete(synchronize_session=False)
    database.delete(community)


def record_community_audit(
    database: Session,
    community: Community,
    *,
    event: str,
    action: str,
    actor_user_id: UUID | None,
    target_user_id: UUID | None = None,
    details: str = "",
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
) -> None:
    """Record one privileged community change in all three places it has to appear.

    `action` keeps the `community_audit_events` vocabulary that the community pages and
    the account export already read. `event` is the shared `domain.action` name, which is
    what the append-only platform log and the outbox are keyed by. The typed row is
    tenant-scoped domain history that cascades with its community; the platform row is the
    cross-domain record that carries the request id and the before/after state.
    """
    scope = community_scope(community.id)
    database.add(
        CommunityAuditEvent(
            community_id=community.id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            action=action,
            details=details,
        )
    )
    record_audit(
        database,
        action=event,
        scope=scope,
        actor_user_id=actor_user_id,
        target_type="user" if target_user_id is not None else "community",
        target_id=target_user_id if target_user_id is not None else community.id,
        before_state=before_state,
        after_state=after_state,
    )
    enqueue_event(
        database,
        event=event,
        aggregate_type="community",
        aggregate_id=community.id,
        scope=scope,
        payload={
            "details": details,
            "target_user_id": str(target_user_id) if target_user_id is not None else None,
        },
    )


def add_moderation_event(
    database: Session,
    community_id: UUID,
    moderator_id: UUID,
    target_type: str,
    target_id: UUID,
    action: str,
    details: str = "",
    *,
    event: str,
) -> None:
    """Record a moderator action.

    `action` stays the `moderation_events` vocabulary the moderator history reads; `event`
    is the shared `domain.action` name recorded in the platform log and the outbox.
    """
    scope = community_scope(community_id)
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
    record_audit(
        database,
        action=event,
        scope=scope,
        actor_user_id=moderator_id,
        target_type=target_type,
        target_id=target_id,
        reason=details,
    )
    enqueue_event(
        database,
        event=event,
        aggregate_type=target_type,
        aggregate_id=target_id,
        scope=scope,
        payload={"details": details, "community_id": str(community_id)},
    )
