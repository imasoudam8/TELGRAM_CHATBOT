"""SQLAlchemy ORM models for user identity, profiles, and preferences."""

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.infrastructure.database.base import Base

if TYPE_CHECKING:
    from src.app.modules.admin.models import AdminUser
    from src.app.modules.wallet.models import Wallet


class User(Base):
    """Root user identity record.

    Contains authentication routing metadata, account status, and authoritative
    adult self-declaration records.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        nullable=False,
    )
    public_id: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    age_18_plus_declared: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    age_declared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    age_declared_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    suspended_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    suspension_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    profile: Mapped["UserProfile | None"] = relationship(
        "UserProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys=(
            "[UserProfile.user_id, UserProfile.age_18_plus_declared, UserProfile.age_declared_date]"
        ),
    )
    preferences: Mapped["UserPreferences | None"] = relationship(
        "UserPreferences",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    wallet: Mapped["Wallet | None"] = relationship(
        "Wallet",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    admin_user: Mapped["AdminUser | None"] = relationship(
        "AdminUser",
        back_populates="user",
        uselist=False,
        foreign_keys="AdminUser.user_id",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "id",
            "age_18_plus_declared",
            "age_declared_date",
            name="uq_users_id_age_declaration",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'BANNED', 'DELETED')",
            name="chk_users_status_valid",
        ),
        CheckConstraint(
            "(age_18_plus_declared = FALSE AND age_declared_at IS NULL "
            "AND age_declared_date IS NULL) OR "
            "(age_18_plus_declared = TRUE AND age_declared_at IS NOT NULL "
            "AND age_declared_date IS NOT NULL)",
            name="chk_users_age_declaration_consistency",
        ),
        CheckConstraint(
            "age_declared_at IS NULL OR age_declared_at >= created_at - INTERVAL '5 seconds'",
            name="chk_users_declaration_timestamp_ordering",
        ),
        Index("idx_users_public_id", "public_id"),
        Index(
            "idx_users_status",
            "status",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )


class UserProfile(Base):
    """Public profile attributes presented during anonymous peer discovery.

    Creation is strictly guarded by composite foreign key against undeclared users.
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )
    age_18_plus_declared: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    age_declared_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )
    birth_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    gender: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    city: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    bio: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )
    completion_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'INCOMPLETE'"),
    )
    visibility: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'PUBLIC'"),
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

    # Relationship
    user: Mapped["User"] = relationship(
        "User",
        back_populates="profile",
        foreign_keys=(
            "[UserProfile.user_id, UserProfile.age_18_plus_declared, UserProfile.age_declared_date]"
        ),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "age_18_plus_declared", "age_declared_date"],
            ["users.id", "users.age_18_plus_declared", "users.age_declared_date"],
            name="fk_user_profiles_users_declaration",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "age_18_plus_declared = TRUE",
            name="chk_user_profiles_age_declared_true",
        ),
        CheckConstraint(
            "birth_date <= (age_declared_date - INTERVAL '18 years')",
            name="chk_user_profiles_birth_date_18_plus",
        ),
        CheckConstraint(
            "gender IN ('MALE', 'FEMALE', 'NON_BINARY', 'OTHER')",
            name="chk_user_profiles_gender_valid",
        ),
        CheckConstraint(
            "completion_status IN ('INCOMPLETE', 'COMPLETED')",
            name="chk_user_profiles_completion_status_valid",
        ),
        CheckConstraint(
            "visibility IN ('PUBLIC', 'ANONYMOUS', 'HIDDEN')",
            name="chk_user_profiles_visibility_valid",
        ),
        Index(
            "idx_profiles_matching",
            "gender",
            "city",
            "completion_status",
            postgresql_where=text("visibility = 'PUBLIC'"),
        ),
    )


class UserPreferences(Base):
    """Matching criteria configured by users for random matchmaking and discovery."""

    __tablename__ = "user_preferences"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    preferred_genders: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("ARRAY[]::TEXT[]"),
    )
    min_age: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("18"),
    )
    max_age: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("99"),
    )
    preferred_cities: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("ARRAY[]::TEXT[]"),
    )
    required_filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    relaxable_filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
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

    # Relationship
    user: Mapped["User"] = relationship(
        "User",
        back_populates="preferences",
        foreign_keys=[user_id],
    )

    __table_args__ = (
        CheckConstraint(
            "min_age >= 18 AND max_age <= 99 AND min_age <= max_age",
            name="chk_user_preferences_age_range",
        ),
    )
