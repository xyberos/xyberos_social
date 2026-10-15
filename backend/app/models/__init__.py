"""SQLAlchemy models, re-exported so `from app.models import X` keeps working."""

from app.models.identity import User, UserSession, PasswordResetToken
from app.models.social import UserRelationship
from app.models.attachments import Attachment
from app.models.audit import PlatformAuditEvent
from app.models.communities import Community, CommunityMembership, CommunityAuditEvent
from app.models.content import Post, Comment, ContentReport, ModerationEvent
from app.models.idempotency import IdempotencyKey
from app.models.messaging import (
    Conversation,
    ConversationMember,
    ConversationMessage,
    ConversationEvent,
)
from app.models.notifications import Notification
from app.models.outbox import OutboxEvent

__all__ = [
    "Attachment",
    "Comment",
    "Community",
    "CommunityAuditEvent",
    "CommunityMembership",
    "ContentReport",
    "Conversation",
    "ConversationEvent",
    "ConversationMember",
    "ConversationMessage",
    "IdempotencyKey",
    "ModerationEvent",
    "Notification",
    "OutboxEvent",
    "PasswordResetToken",
    "PlatformAuditEvent",
    "Post",
    "User",
    "UserRelationship",
    "UserSession",
]
