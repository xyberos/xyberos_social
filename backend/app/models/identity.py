"""Accounts, sessions, and recovery tokens."""

from datetime import datetime

from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    func,
)

from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "updates_source IN ('all', 'people', 'communities')", name="ck_user_updates_source"
        ),
        CheckConstraint("updates_days IN (1, 7, 30, 90)", name="ck_user_updates_days"),
        CheckConstraint("updates_limit IN (1, 5, 10, 20, 50)", name="ck_user_updates_limit"),
        CheckConstraint("theme IN ('system', 'light', 'dark')", name="ck_user_theme"),
        CheckConstraint(
            "appearance_preset IN ('forest', 'ocean', 'violet', 'sunset', 'midnight')",
            name="ck_user_appearance_preset",
        ),
        CheckConstraint(
            "background_style IN ('paper', 'mist', 'dawn', 'aurora')",
            name="ck_user_background_style",
        ),
        CheckConstraint(
            "display_density IN ('comfortable', 'compact')", name="ck_user_display_density"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(80))
    bio: Mapped[str] = mapped_column(Text, default="", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # A platform role, not a community one: it unlocks the queue for personal updates, which
    # have no community to moderate them. An operator grants it; nothing in the application
    # grants it to itself.
    is_moderator: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    quiet_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notify_replies: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_moderation: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notify_calls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # "Who can call me": false means no invite is created for this account at all. It is a
    # settings-model decision, checked at ring time, and it never hides an existing call.
    accept_calls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Follows the device until the reader chooses; the cookie mirrors this for signed-out pages.
    theme: Mapped[str] = mapped_column(String(8), default="system", nullable=False)
    appearance_preset: Mapped[str] = mapped_column(String(16), default="forest", nullable=False)
    background_style: Mapped[str] = mapped_column(String(16), default="paper", nullable=False)
    display_density: Mapped[str] = mapped_column(String(16), default="comfortable", nullable=False)
    avatar_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)

    @property
    def avatar_url(self) -> str | None:
        """The versioned public URL, or `None` when the initial should be shown.

        The storage key is unique per upload, so including its last segment makes a
        replacement a new URL and no browser serves a previously cached image.
        """
        if not self.avatar_key:
            return None
        version = self.avatar_key.rsplit("/", 1)[-1]
        return f"/avatars/{self.id}?v={version}"

    updates_source: Mapped[str] = mapped_column(String(16), default="all", nullable=False)
    updates_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    updates_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User | None] = relationship(back_populates="sessions")


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
