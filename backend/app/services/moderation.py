"""What a moderation decision leaves behind, and what an author may question.

An appeal answers a specific decision, so the report that produced it is part of the rule: a
community moderator's removal stays with that community's owners, and the platform role does
not overrule it.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ContentReport, ModerationAppeal


def platform_removal_report(database: Session, target_type: str, target_id: UUID):
    """The resolved platform report that removed this content, if that is what happened."""
    return database.scalar(
        select(ContentReport)
        .where(
            ContentReport.community_id.is_(None),
            ContentReport.target_type == target_type,
            ContentReport.target_id == target_id,
            ContentReport.status == "resolved",
        )
        .order_by(ContentReport.resolved_at.desc())
    )


def appeal_for(
    database: Session, target_type: str, target_id: UUID, author_id: UUID
) -> ModerationAppeal | None:
    return database.scalar(
        select(ModerationAppeal).where(
            ModerationAppeal.target_type == target_type,
            ModerationAppeal.target_id == target_id,
            ModerationAppeal.author_id == author_id,
        )
    )
