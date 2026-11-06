"""SQLAlchemy ORM models for dynamic system configuration, admin users, and audit logs."""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.infrastructure.database.base import Base

if TYPE_CHECKING:
    from src.app.modules.users.models import User


class SystemConfiguration(Base):
    """Durable key-value store for application parameters and business thresholds."""

    __tablename__ = "system_configurations"

    key: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    value: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    category: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    updated_by: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # Relationship
    updater: Mapped["User"] = relationship("User", foreign_keys=[updated_by])

    __table_args__ = (
        CheckConstraint(
            "category IN ('FINANCIAL', 'CHAT', 'MEDIA', 'SYSTEM')",
            name="chk_system_configurations_category_valid",
        ),
        CheckConstraint(
            "key <> 'video_size_limit_bytes' OR ("
            "jsonb_typeof(value) = 'object' AND "
            "value ? 'limit' AND "
            "jsonb_typeof(value->'limit') = 'number' AND "
            "(value->>'limit') ~ '^[1-9][0-9]{0,7}$' AND "
            "(value->>'limit')::integer <= 10485760)",
            name="chk_system_configurations_video_size_ceiling",
        ),
        CheckConstraint(
            "key <> 'message_retention_days' OR ("
            "jsonb_typeof(value) = 'object' AND "
            "value ? 'days' AND "
            "jsonb_typeof(value->'days') = 'number' AND "
            "(value->>'days') ~ '^[1-9][0-9]{0,1}$' AND "
            "(value->>'days')::integer <= 30)",
            name="chk_system_configurations_retention_days_ceiling",
        ),
    )


class AdminUser(Base):
    """Role-based privilege assignment for internal operators."""

    __tablename__ = "admin_users"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    assigned_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
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
    user: Mapped["User"] = relationship("User", back_populates="admin_user", foreign_keys=[user_id])
    assigner: Mapped["User | None"] = relationship("User", foreign_keys=[assigned_by])

    __table_args__ = (
        CheckConstraint(
            "role IN ('SUPER_ADMIN', 'SUPPORT', 'MODERATOR')",
            name="chk_admin_users_role_valid",
        ),
    )


class AdminAuditLog(Base):
    """Tamper-resistant audit log capturing all sensitive administrative operations."""

    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    admin_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    payload_before: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    payload_after: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # Relationship
    admin_user: Mapped["User"] = relationship("User", foreign_keys=[admin_user_id])

    __table_args__ = (
        Index("idx_admin_audit_logs_search", "admin_user_id", "action", text("created_at DESC")),
    )
