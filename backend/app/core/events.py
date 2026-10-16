"""The `domain.action` event vocabulary shared by the social and commercial domains.

Audit records and outbox events name their action from this module so social and
commercial events occupy one namespace instead of two dialects. The commercial names are
declared here ahead of the phases that emit them for the same reason: the vocabulary is
what makes a derived index rebuildable, so it has to be fixed before writers exist.

Names are `domain.action` or `domain.subject.action`, lower case, dot separated.
"""

import re

EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
MAX_EVENT_NAME_LENGTH = 64

# Identity and account lifecycle.
ACCOUNT_DELETED = "account.deleted"

# Communities and their membership.
COMMUNITY_CREATED = "community.created"
COMMUNITY_DELETED = "community.deleted"
COMMUNITY_MEMBER_INVITED = "community.member.invited"
COMMUNITY_MEMBER_REMOVED = "community.member.removed"
COMMUNITY_ROLE_CHANGED = "community.role.changed"
COMMUNITY_OWNERSHIP_TRANSFERRED = "community.ownership.transferred"

# Content and moderation.
POST_CREATED = "post.created"
POST_REMOVED = "post.removed"
POST_LOCKED = "post.locked"
POST_UNLOCKED = "post.unlocked"
POST_REPORTED = "post.reported"
POST_AUDIENCE_CHANGED = "post.audience.changed"
COMMENT_CREATED = "comment.created"
COMMENT_REMOVED = "comment.removed"
COMMENT_REPORTED = "comment.reported"
REPORT_RESOLVED = "report.resolved"
REPORT_DISMISSED = "report.dismissed"

# Conversations and messages.
CONVERSATION_CREATED = "conversation.created"
CONVERSATION_MEMBER_ADDED = "conversation.member.added"
CONVERSATION_MEMBER_REMOVED = "conversation.member.removed"
CONVERSATION_MEMBER_LEFT = "conversation.member.left"
MESSAGE_CREATED = "message.created"

SOCIAL_EVENTS = (
    ACCOUNT_DELETED,
    COMMUNITY_CREATED,
    COMMUNITY_DELETED,
    COMMUNITY_MEMBER_INVITED,
    COMMUNITY_MEMBER_REMOVED,
    COMMUNITY_ROLE_CHANGED,
    COMMUNITY_OWNERSHIP_TRANSFERRED,
    POST_CREATED,
    POST_REMOVED,
    POST_LOCKED,
    POST_UNLOCKED,
    POST_REPORTED,
    POST_AUDIENCE_CHANGED,
    COMMENT_CREATED,
    COMMENT_REMOVED,
    COMMENT_REPORTED,
    REPORT_RESOLVED,
    REPORT_DISMISSED,
    CONVERSATION_CREATED,
    CONVERSATION_MEMBER_ADDED,
    CONVERSATION_MEMBER_REMOVED,
    CONVERSATION_MEMBER_LEFT,
    MESSAGE_CREATED,
)

# Reserved for Phases 11-14: named now so the namespace stays single and the commercial
# writers do not invent a second convention.
COMMERCIAL_EVENTS = (
    "business.verification.completed",
    "offering.published",
    "order.completed",
)


def is_event_name(name: str) -> bool:
    return bool(name) and len(name) <= MAX_EVENT_NAME_LENGTH and bool(
        EVENT_NAME_PATTERN.match(name)
    )


def validate_event_name(name: str) -> str:
    """Reject a malformed name at the write site instead of storing an unreadable event."""
    if not is_event_name(name):
        raise ValueError(f"{name!r} is not a domain.action event name")
    return name


def domain_of(name: str) -> str:
    return name.split(".", 1)[0]
