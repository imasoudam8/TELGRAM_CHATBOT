"""Data transfer objects and domain exceptions for the wallet and settlement module."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime


class WalletError(Exception):
    """Base exception for wallet domain errors."""


class InvalidAmountError(WalletError, ValueError):
    """Raised when a financial operation specifies a non-positive or invalid amount."""


class InsufficientBalanceError(WalletError, RuntimeError):
    """Raised when a wallet has fewer spendable coins than required for a debit."""


class WalletPermissionError(WalletError, PermissionError):
    """Raised when an actor or user is not eligible or authorized for a wallet action."""


@dataclass(frozen=True)
class WalletBalanceDTO:
    """Snapshot of a user's durable purchased coins and today's non-expired free coins."""

    user_id: int
    purchased_coins: int
    free_coins: int
    total_spendable_coins: int
    free_allowance_id: int | None
    free_allowance_date: date | None
    free_expires_at: datetime | None


@dataclass(frozen=True)
class LedgerEntryDTO:
    """Immutable transaction journal entry representation."""

    id: int
    wallet_user_id: int
    idempotency_key: str
    transaction_type: str
    credit_type: str
    allowance_id: int | None
    allowance_date: date | None
    amount: int
    purchased_balance_before: int
    purchased_balance_after: int
    free_balance_before: int
    free_balance_after: int
    reference_id: str | None
    actor_type: str
    actor_id: int | None
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class TransactionHistoryPageDTO:
    """Paginated immutable ledger history page."""

    items: tuple[LedgerEntryDTO, ...]
    total_count: int
    offset: int
    limit: int
    has_next: bool
    has_prev: bool


@dataclass(frozen=True)
class SettlementResult:
    """Outcome of attempting atomic chat settlement."""

    chat_id: uuid.UUID
    status: str  # 'SETTLED', 'ALREADY_CHARGED', 'NOT_ELIGIBLE', 'INSUFFICIENT_BALANCE'
    chat_type: str
    charged_at: datetime | None
    total_coins_charged: int
    coins_by_user: dict[int, int]
    ledger_ids: tuple[int, ...] = ()
    insufficient_user_ids: tuple[int, ...] = ()
    required_coins_by_user: dict[int, int] | None = None
    available_coins_by_user: dict[int, int] | None = None
    reason: str | None = None
