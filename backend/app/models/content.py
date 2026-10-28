"""Community discussions and personal updates, threaded comments, reports, and moderation."""

from datetime import datetime

from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)

from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Who may read a personal update. A community post carries the default: its audience is the
# community's membership rather than a choice made on the post itself. `public` is the only
# value a signed-out reader can satisfy, so it is the only one a crawler ever sees.
AUDIENCES = ("only_me", "followers", "everyone", "public")


class Post(Base):
    """A post belongs to a community, or to an account when it is a personal update.

    The two shapes are mutually exclusive and the database enforces which fields each one
    carries: a community post is titled, a personal update is text with an optional title.
    A personal update's text may be empty, because media can be the whole update; the route
    still refuses a post that has neither text nor media.
    """

    __tablename__ = "posts"
    __table_args__ = (
        CheckConstraint(
            "(community_id IS NOT NULL AND title IS NOT NULL"
            " AND length(title) BETWEEN 1 AND 140)"
            " OR (community_id IS NULL AND length(body) <= 10000)",
            name="ck_post_shape",
        ),
        CheckConstraint("title IS NULL OR length(title) <= 140", name="ck_post_title_length"),
        CheckConstraint("length(body) <= 10000", name="ck_post_body_length"),
        CheckConstraint(
            "audience IN ('only_me', 'followers', 'everyone', 'public')", name="ck_post_audience"
        ),
        CheckConstraint(
            "community_id IS NULL OR audience = 'followers'", name="ck_post_audience_scope"
        ),
        Index("ix_posts_community_created", "community_id", "created_at"),
        Index("ix_posts_author_created", "author_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    community_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("communities.id", ondelete="CASCADE"), nullable=True
    )
    author_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(140))
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    audience: Mapped[str] = mapped_column(String(16), default="followers", nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (
        CheckConstraint("length(body) BETWEEN 1 AND 5000", name="ck_comment_body_length"),
        Index("ix_comments_post_created", "post_id", "created_at"),
        Index("ix_comments_parent", "parent_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    post_id: Mapped[UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ContentReport(Base):
    __tablename__ = "content_reports"
    __table_args__ = (
        CheckConstraint("target_type IN ('post', 'comment')", name="ck_report_target_type"),
        CheckConstraint("status IN ('open', 'resolved', 'dismissed')", name="ck_report_status"),
        CheckConstraint("length(reason) BETWEEN 1 AND 1000", name="ck_report_reason_length"),
        UniqueConstraint("reporter_id", "target_type", "target_id", name="uq_reporter_target"),
        Index("ix_reports_community_status_created", "community_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    # Null names a platform report: the target is a personal update, which belongs to an
    # account rather than a community, so there is no community queue to file it in.
    community_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("communities.id", ondelete="CASCADE"), nullable=True
    )
    reporter_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(8))
    target_id: Mapped[UUID] = mapped_column(Uuid)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="open", nullable=False)
    resolved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ModerationEvent(Base):
    __tablename__ = "moderation_events"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('post', 'comment', 'report')", name="ck_moderation_target_type"
        ),
        Index("ix_moderation_community_created", "community_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    community_id: Mapped[UUID] = mapped_column(
        ForeignKey("communities.id", ondelete="CASCADE"), nullable=False
    )
    moderator_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    target_type: Mapped[str] = mapped_column(String(8))
    target_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(24))
    details: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ModerationAppeal(Base):
    """An author's request that a platform moderator review a removal.

    The target is named by id without a foreign key, like the audit log, so the record of an
    appeal outlives the content it is about. `report_id` names the decision being appealed: a
    community moderator's removal has its own owners and is not reviewable here.
    """

    __tablename__ = "moderation_appeals"
    __table_args__ = (
        CheckConstraint("target_type IN ('post', 'comment')", name="ck_appeal_target_type"),
        CheckConstraint("status IN ('pending', 'granted', 'denied')", name="ck_appeal_status"),
        CheckConstraint("length(reason) BETWEEN 1 AND 2000", name="ck_appeal_reason_length"),
        # One appeal per removal. A denied appeal is the end of that decision, not a loop.
        UniqueConstraint("author_id", "target_type", "target_id", name="uq_appeal_target"),
        Index("ix_appeals_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    author_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    report_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("content_reports.id", ondelete="SET NULL"), nullable=True
    )
    target_type: Mapped[str] = mapped_column(String(8))
    target_id: Mapped[UUID] = mapped_column(Uuid)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    resolved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PostReaction(Base):
    """One reader's verdict on one post: a like, a dislike, or nothing at all.

    The pair is the primary key, so a reader cannot hold two verdicts on the same post and
    switching is an update rather than a second row. Counts are derived from this table rather
    than stored beside the post, so no counter can drift away from the rows that justify it.
    """

    __tablename__ = "post_reactions"
    __table_args__ = (
        CheckConstraint("kind IN ('like', 'dislike')", name="ck_reaction_kind"),
        Index("ix_reactions_post_kind", "post_id", "kind"),
    )

    post_id: Mapped[UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
