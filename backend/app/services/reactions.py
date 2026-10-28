"""Let a reader like or dislike a post, and count the verdicts.

One row per reader per post, so a case of the same verdict twice is the second click undoing
the first, and a change of mind is an update rather than a third row. There is no counter column
to drift: the numbers a page shows are counted from these rows.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PostReaction

LIKE = "like"
DISLIKE = "dislike"
REACTION_KINDS = (LIKE, DISLIKE)


def summaries(
    database: Session, post_ids, viewer_id: UUID | None = None
) -> dict[UUID, dict]:
    """Likes, dislikes, and the viewer's own verdict for a page of posts, in two queries."""
    wanted = list(dict.fromkeys(post_ids))
    summary = {
        post_id: {"likes": 0, "dislikes": 0, "mine": None} for post_id in wanted
    }
    if not wanted:
        return summary
    counts = database.execute(
        select(PostReaction.post_id, PostReaction.kind, func.count())
        .where(PostReaction.post_id.in_(wanted))
        .group_by(PostReaction.post_id, PostReaction.kind)
    ).all()
    for post_id, kind, total in counts:
        summary[post_id]["likes" if kind == LIKE else "dislikes"] = total
    if viewer_id is not None:
        mine = database.execute(
            select(PostReaction.post_id, PostReaction.kind).where(
                PostReaction.post_id.in_(wanted), PostReaction.user_id == viewer_id
            )
        ).all()
        for post_id, kind in mine:
            summary[post_id]["mine"] = kind
    return summary


def apply(database: Session, *, post_id: UUID, user_id: UUID, kind: str) -> str | None:
    """Record a verdict and report the one that now stands, or `None` when it was withdrawn."""
    existing = database.get(PostReaction, (post_id, user_id))
    if existing is None:
        database.add(PostReaction(post_id=post_id, user_id=user_id, kind=kind))
        return kind
    if existing.kind == kind:
        database.delete(existing)
        return None
    existing.kind = kind
    return kind
