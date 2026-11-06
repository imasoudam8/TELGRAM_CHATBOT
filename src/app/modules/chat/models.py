"""SQLAlchemy ORM models for chat, requests, connections, messages, and holds."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.infrastructure.database.base import Base

if TYPE_CHECKING:
    from src.app.modules.admin.models import AdminUser
    from src.app.modules.moderation.models import Report
    from src.app.modules.users.models import User


class DirectRequest(Base):
    """Direct chat invitations initiated from user profile views or shared public links."""

    __tablename__ = "direct_requests"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    requester_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'PENDING'"),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    requester: Mapped["User"] = relationship("User", foreign_keys=[requester_id])
    target: Mapped["User"] = relationship("User", foreign_keys=[target_id])

    __table_args__ = (
        UniqueConstraint(
            "id",
            "status",
            "requester_id",
            "target_id",
            name="uq_direct_requests_composite_state",
        ),
        CheckConstraint(
            "requester_id <> target_id",
            name="chk_direct_requests_different_users",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'EXPIRED', 'CANCELLED')",
            name="chk_direct_requests_status_valid",
        ),
        Index(
            "uq_direct_requests_pending_pair",
            "requester_id",
            "target_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "idx_direct_requests_inbox",
            "target_id",
            "status",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "idx_direct_requests_expiry",
            "status",
            "expires_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )


class UserConnection(Base):
    """Permanent bilateral contact address book supporting asymmetric removal."""

    __tablename__ = "user_connections"

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
    connected_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    established_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    connected_user: Mapped["User"] = relationship("User", foreign_keys=[connected_user_id])

    __table_args__ = (
        UniqueConstraint("user_id", "connected_user_id", name="uq_user_connections_pair"),
        CheckConstraint(
            "user_id <> connected_user_id",
            name="chk_user_connections_different_users",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'REMOVED')",
            name="chk_user_connections_status_valid",
        ),
        Index(
            "idx_user_connections_list",
            "user_id",
            "status",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )


class UserBlock(Base):
    """Bidirectional communication prohibition."""

    __tablename__ = "user_blocks"

    blocker_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    blocked_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    reason: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # Relationships
    blocker: Mapped["User"] = relationship("User", foreign_keys=[blocker_id])
    blocked: Mapped["User"] = relationship("User", foreign_keys=[blocked_id])

    __table_args__ = (
        CheckConstraint("blocker_id <> blocked_id", name="chk_user_blocks_different_users"),
        Index("idx_user_blocks_reverse", "blocked_id", "blocker_id"),
    )


class ChatSession(Base):
    """Captures session lifecycle, participation records, charging status, and termination reasons.

    Enforces origin from an ACCEPTED direct request for direct chats.
    """

    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    chat_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    direct_request_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    direct_request_status: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    participant_one_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    participant_two_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'INITIATED'"),
    )
    participant_one_sent_msg: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    participant_two_sent_msg: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    is_charged: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    charged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    end_reason: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    participant_one: Mapped["User"] = relationship("User", foreign_keys=[participant_one_id])
    participant_two: Mapped["User"] = relationship("User", foreign_keys=[participant_two_id])
    direct_request: Mapped["DirectRequest | None"] = relationship(
        "DirectRequest",
        foreign_keys=(
            "[ChatSession.direct_request_id, ChatSession.direct_request_status, "
            "ChatSession.participant_one_id, ChatSession.participant_two_id]"
        ),
        overlaps="participant_one,participant_two",
    )
    messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage",
        back_populates="chat_session",
        passive_deletes=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            [
                "direct_request_id",
                "direct_request_status",
                "participant_one_id",
                "participant_two_id",
            ],
            [
                "direct_requests.id",
                "direct_requests.status",
                "direct_requests.requester_id",
                "direct_requests.target_id",
            ],
            name="fk_chat_sessions_direct_request_accepted",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "participant_one_id <> participant_two_id",
            name="chk_chat_sessions_different_participants",
        ),
        CheckConstraint(
            "chat_type IN ('RANDOM', 'DIRECT')",
            name="chk_chat_sessions_type_valid",
        ),
        CheckConstraint(
            "(chat_type = 'RANDOM' AND direct_request_id IS NULL "
            "AND direct_request_status IS NULL) OR "
            "(chat_type = 'DIRECT' AND direct_request_id IS NOT NULL "
            "AND direct_request_status = 'ACCEPTED')",
            name="chk_chat_sessions_direct_linkage_rules",
        ),
        CheckConstraint(
            "status IN ('INITIATED', 'ACTIVE', 'ENDED')",
            name="chk_chat_sessions_status_valid",
        ),
        Index(
            "idx_chat_sessions_history",
            "participant_one_id",
            "participant_two_id",
            "status",
        ),
        Index(
            "idx_chat_sessions_direct_request",
            "direct_request_id",
            postgresql_where=text("direct_request_id IS NOT NULL"),
        ),
    )


class ActiveChatParticipant(Base):
    """Junction table enforcing the 'One active chat per user' invariant at the schema level."""

    __tablename__ = "active_chat_participants"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    chat_session: Mapped["ChatSession"] = relationship("ChatSession", foreign_keys=[chat_id])


class ChatMessage(Base):
    """Stores message metadata and Telegram media identifiers without local persistence."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sender_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recipient_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    message_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    telegram_file_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    file_size_bytes: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    duration_seconds: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    # Relationships
    chat_session: Mapped["ChatSession"] = relationship("ChatSession", back_populates="messages")
    sender: Mapped["User"] = relationship("User", foreign_keys=[sender_id])
    recipient: Mapped["User"] = relationship("User", foreign_keys=[recipient_id])

    __table_args__ = (
        UniqueConstraint("id", "chat_id", name="uq_chat_messages_id_chat"),
        CheckConstraint(
            "message_type IN ('TEXT', 'PHOTO', 'VOICE', 'VIDEO', 'STICKER', 'ANIMATION')",
            name="chk_chat_messages_type_valid",
        ),
        CheckConstraint(
            "message_type <> 'VIDEO' OR file_size_bytes < 10485760",
            name="chk_chat_messages_video_size_ceiling",
        ),
        CheckConstraint(
            "expires_at <= created_at + INTERVAL '30 days'",
            name="chk_chat_messages_retention_ceiling",
        ),
        Index("idx_chat_messages_chat_time", "chat_id", "created_at"),
        Index("idx_chat_messages_cleanup", "expires_at"),
    )


class MessageEvidenceHold(Base):
    """Normalized evidence-hold relationship supporting multiple concurrent reports and legal holds.

    Preserves durable moderation and legal audit history without retaining expired message content.
    """

    __tablename__ = "message_evidence_holds"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    chat_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    message_content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    report_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reports.id", ondelete="RESTRICT"),
        nullable=True,
    )
    legal_reference_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    hold_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    created_by_admin_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("admin_users.user_id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_by_system_actor: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    released_by_admin_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("admin_users.user_id", ondelete="RESTRICT"),
        nullable=True,
    )
    released_by_system_actor: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    release_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # Relationships
    chat_session: Mapped["ChatSession"] = relationship("ChatSession")
    message: Mapped["ChatMessage | None"] = relationship("ChatMessage")
    report: Mapped["Report | None"] = relationship("Report")
    created_by_admin: Mapped["AdminUser | None"] = relationship(
        "AdminUser", foreign_keys=[created_by_admin_id]
    )
    released_by_admin: Mapped["AdminUser | None"] = relationship(
        "AdminUser", foreign_keys=[released_by_admin_id]
    )

    __table_args__ = (
        CheckConstraint(
            "hold_type IN ('USER_REPORT', 'LEGAL_HOLD', 'SAFETY_AUDIT')",
            name="chk_evidence_holds_type_valid",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'RELEASED')",
            name="chk_evidence_holds_status_valid",
        ),
        CheckConstraint(
            "(created_by_admin_id IS NOT NULL AND created_by_system_actor IS NULL) OR "
            "(created_by_admin_id IS NULL AND created_by_system_actor IS NOT NULL)",
            name="chk_evidence_holds_creator_attribution",
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND released_at IS NULL AND released_by_admin_id IS NULL "
            "AND released_by_system_actor IS NULL AND release_notes IS NULL) OR "
            "(status = 'RELEASED' AND released_at IS NOT NULL AND release_notes IS NOT NULL AND "
            "((released_by_admin_id IS NOT NULL AND released_by_system_actor IS NULL) OR "
            "(released_by_admin_id IS NULL AND released_by_system_actor IS NOT NULL)))",
            name="chk_evidence_holds_release_consistency",
        ),
        CheckConstraint(
            "(hold_type = 'LEGAL_HOLD' AND legal_reference_id IS NOT NULL "
            "AND report_id IS NULL) OR "
            "(hold_type = 'USER_REPORT' AND report_id IS NOT NULL "
            "AND legal_reference_id IS NULL) OR "
            "(hold_type = 'SAFETY_AUDIT')",
            name="chk_evidence_holds_type_attributes",
        ),
        Index(
            "uq_evidence_holds_message_report",
            "message_id",
            "report_id",
            unique=True,
            postgresql_where=text("message_id IS NOT NULL AND report_id IS NOT NULL"),
        ),
        Index(
            "uq_evidence_holds_message_legal",
            "message_id",
            "legal_reference_id",
            unique=True,
            postgresql_where=text("message_id IS NOT NULL AND legal_reference_id IS NOT NULL"),
        ),
        Index(
            "idx_evidence_holds_active",
            "message_id",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "idx_evidence_holds_report",
            "report_id",
            postgresql_where=text("report_id IS NOT NULL"),
        ),
        Index(
            "idx_evidence_holds_legal",
            "legal_reference_id",
            postgresql_where=text("legal_reference_id IS NOT NULL"),
        ),
    )
