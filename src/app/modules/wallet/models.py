"""SQLAlchemy ORM models for wallets, daily allowances, and the append-only ledger."""

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.infrastructure.database.base import Base

if TYPE_CHECKING:
    from src.app.modules.users.models import User


class Wallet(Base):
    """Stores durable purchased coins that never expire."""

    __tablename__ = "wallets"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    purchased_coins: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
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
    user: Mapped["User"] = relationship("User", back_populates="wallet", foreign_keys=[user_id])

    __table_args__ = (
        CheckConstraint("purchased_coins >= 0", name="chk_wallets_purchased_coins_non_negative"),
    )


class WalletDailyAllowance(Base):
    """Isolates daily promotional free credits expiring at Tehran midnight."""

    __tablename__ = "wallet_daily_allowances"

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
    allowance_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )
    allocated_credits: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    used_credits: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    remaining_credits: Mapped[int] = mapped_column(
        Integer,
        Computed("allocated_credits - used_credits", persisted=True),
        nullable=False,
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        UniqueConstraint("user_id", "allowance_date", name="uq_wallet_daily_allowances_user_date"),
        UniqueConstraint(
            "id",
            "user_id",
            "allowance_date",
            name="uq_wallet_daily_allowances_composite_lineage",
        ),
        CheckConstraint(
            "allocated_credits >= 0",
            name="chk_daily_allowance_allocated_non_negative",
        ),
        CheckConstraint(
            "used_credits >= 0 AND used_credits <= allocated_credits",
            name="chk_daily_allowance_used_bounds",
        ),
    )


class WalletLedger(Base):
    """Append-only, single-table transaction journal (not double-entry).

    Every balance modification produces corresponding immutable ledger entries.
    """

    __tablename__ = "wallet_ledgers"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    wallet_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        nullable=False,
    )
    transaction_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    credit_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    allowance_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    allowance_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )
    amount: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    purchased_balance_before: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    purchased_balance_after: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    free_balance_before: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    free_balance_after: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    reference_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    actor_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
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

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[wallet_user_id])
    actor: Mapped["User | None"] = relationship("User", foreign_keys=[actor_id])
    allowance: Mapped["WalletDailyAllowance | None"] = relationship(
        "WalletDailyAllowance",
        foreign_keys=(
            "[WalletLedger.allowance_id, WalletLedger.wallet_user_id, WalletLedger.allowance_date]"
        ),
        overlaps="user",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["allowance_id", "wallet_user_id", "allowance_date"],
            [
                "wallet_daily_allowances.id",
                "wallet_daily_allowances.user_id",
                "wallet_daily_allowances.allowance_date",
            ],
            name="fk_wallet_ledgers_allowance_lineage",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "credit_type IN ('FREE', 'PURCHASED')",
            name="chk_wallet_ledgers_credit_type_valid",
        ),
        CheckConstraint(
            "purchased_balance_before >= 0 AND purchased_balance_after >= 0",
            name="chk_wallet_ledgers_purchased_balance_bounds",
        ),
        CheckConstraint(
            "free_balance_before >= 0 AND free_balance_after >= 0",
            name="chk_wallet_ledgers_free_balance_bounds",
        ),
        CheckConstraint(
            "(credit_type = 'FREE' AND allowance_id IS NOT NULL AND allowance_date IS NOT NULL) OR "
            "(credit_type = 'PURCHASED' AND allowance_id IS NULL AND allowance_date IS NULL)",
            name="chk_wallet_ledgers_allowance_consistency",
        ),
        Index("idx_wallet_ledgers_user", "wallet_user_id", text("created_at DESC")),
        Index(
            "idx_wallet_ledgers_allowance",
            "allowance_id",
            postgresql_where=text("allowance_id IS NOT NULL"),
        ),
    )
