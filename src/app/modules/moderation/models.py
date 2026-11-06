"""SQLAlchemy ORM models for reports and moderation actions."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.infrastructure.database.base import Base

if TYPE_CHECKING:
    from src.app.modules.chat.models import ChatMessage, ChatSession
    from src.app.modules.users.models import User


class Report(Base):
    """User-submitted grievances regarding abusive conduct or policy violations."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    reporter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reported_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    chat_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    reported_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    category: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'PENDING'"),
    )
    moderator_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    resolution_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # Relationships
    reporter: Mapped["User"] = relationship("User", foreign_keys=[reporter_id])
    reported_user: Mapped["User"] = relationship("User", foreign_keys=[reported_user_id])
    moderator: Mapped["User | None"] = relationship("User", foreign_keys=[moderator_id])
    chat_session: Mapped["ChatSession | None"] = relationship(
        "ChatSession", foreign_keys=[chat_session_id]
    )
    reported_message: Mapped["ChatMessage | None"] = relationship(
        "ChatMessage", foreign_keys=[reported_message_id]
    )

    __table_args__ = (
        CheckConstraint("reporter_id <> reported_user_id", name="chk_reports_different_users"),
        CheckConstraint(
            "category IN ('HARASSMENT', 'SPAM', 'INAPPROPRIATE_CONTENT', "
            "'SCAM_FRAUD', 'UNDERAGE_SUSPICION', 'OTHER')",
            name="chk_reports_category_valid",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'REVIEWED', 'ACTIONED', 'DISMISSED')",
            name="chk_reports_status_valid",
        ),
        Index(
            "idx_reports_queue",
            "status",
            "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )


class ModerationAction(Base):
    """Sanctions and corrective measures issued against offending users."""

    __tablename__ = "moderation_actions"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    action_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    moderator_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    report_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reports.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    moderator: Mapped["User"] = relationship("User", foreign_keys=[moderator_id])
    report: Mapped["Report | None"] = relationship("Report", foreign_keys=[report_id])

    __table_args__ = (
        CheckConstraint(
            "action_type IN ('WARNING', 'TEMPORARY_SUSPENSION', "
            "'PERMANENT_BAN', 'CHAT_RESTRICTION')",
            name="chk_moderation_actions_type_valid",
        ),
        Index("idx_moderation_actions_user", "user_id", "expires_at"),
    )
